"""Independent boundary cases for training-only grouped model validation."""

import copy
import unittest

from ds_m01.model_splits import build_fold_memberships, default_folds, validate_folds


def config():
    return {
        "timezone": "America/Los_Angeles",
        "split": {"train_departure_dates": ["2026-01-01", "2026-06-23"],
                  "train_label_cutoff_exclusive": "2026-07-01T00:00:00-07:00",
                  "group_by": ["tenant_id", "trip_id"]},
        "heldout": {"vehicle_ids": ["VEHICLE-H"], "route_ids": ["ROUTE-H"]},
    }


def trip(identifier, departure, role="train", tenant="T1", **fields):
    return dict(tenant_id=tenant, trip_id=identifier, departure_time=departure,
                split_role=role, **fields)


class ModelSplitTests(unittest.TestCase):
    def memberships(self, trips, folds=None):
        return build_fold_memberships(trips, default_folds() if folds is None else folds, config())

    def test_default_fold_cutoffs_respect_dst_and_original_m2(self):
        folds = validate_folds(default_folds(), config())
        self.assertEqual(folds[0]["training_cutoff_at"], "2026-03-01T08:00:00Z")
        self.assertEqual(folds[0]["validation_label_cutoff_at"], "2026-04-01T07:00:00Z")
        self.assertEqual(folds[-1]["validation_label_cutoff_at"], "2026-07-01T07:00:00Z")

    def test_inclusive_local_date_boundary_then_buffer(self):
        rows = self.memberships([
            trip("fit-last", "2026-02-22T07:59:59Z"),
            trip("buffer-first", "2026-02-22T08:00:00Z"),
            trip("val-first", "2026-03-01T08:00:00Z"),
            trip("val-last", "2026-03-25T06:59:59Z"),
            trip("buffer-after", "2026-03-25T07:00:00Z"),
        ], default_folds()[:1])
        self.assertEqual({r["trip_id"]: r["fold_role"] for r in rows},
                         {"fit-last": "fit", "val-first": "validation", "val-last": "validation"})

    def test_spring_dst_utc_dates_do_not_override_local_dates(self):
        rows = self.memberships([
            trip("before-dst", "2026-03-08T09:59:59Z"),
            trip("after-dst", "2026-03-08T10:00:00Z"),
        ], default_folds()[:1])
        self.assertEqual({r["departure_date"] for r in rows}, {"2026-03-08"})
        self.assertEqual({r["fold_role"] for r in rows}, {"validation"})

    def test_protected_and_excluded_roles_never_receive_membership(self):
        rows = self.memberships([
            trip("cal", "2026-07-01T12:00:00-07:00", "calibration"),
            trip("test", "2026-08-01T12:00:00-07:00", "test"),
            trip("held", "2026-03-10T12:00:00-07:00", "excluded"),
            trip("keep", "2026-03-10T12:00:00-07:00"),
        ])
        self.assertTrue(rows)
        self.assertEqual({r["trip_id"] for r in rows}, {"keep"})

    def test_whole_trip_moves_to_later_fit_without_same_fold_overlap(self):
        rows = self.memberships([trip("same", "2026-03-10T12:00:00-07:00")])
        self.assertEqual([(r["fold_id"], r["fold_role"]) for r in rows],
                         [("F01", "validation"), ("F02", "fit"), ("F03", "fit"), ("F04", "fit")])
        self.assertEqual(len({(r["fold_id"], r["tenant_id"], r["trip_id"]) for r in rows}), len(rows))

    def test_fit_and_validation_receive_distinct_exclusive_label_cutoffs(self):
        rows = self.memberships([
            trip("fit", "2026-01-15T12:00:00-08:00"),
            trip("val", "2026-03-10T12:00:00-07:00"),
        ], default_folds()[:1])
        cutoffs = {r["fold_role"]: r["label_cutoff_at"] for r in rows}
        self.assertEqual(cutoffs, {"fit": "2026-03-01T08:00:00Z", "validation": "2026-04-01T07:00:00Z"})

    def test_tenant_is_part_of_group_key(self):
        rows = self.memberships([
            trip("same-id", "2026-01-10T00:00:00-08:00", tenant="T1"),
            trip("same-id", "2026-01-10T00:00:00-08:00", tenant="T2"),
        ], default_folds()[:1])
        self.assertEqual(len(rows), 2)

    def test_duplicate_group_is_rejected_even_if_identical(self):
        row = trip("duplicate", "2026-01-10T00:00:00-08:00")
        with self.assertRaisesRegex(ValueError, "Duplicate tenant/trip"):
            self.memberships([row, copy.deepcopy(row)])

    def test_unknown_roles_and_naive_departure_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown original split"):
            self.memberships([trip("x", "2026-01-10T00:00:00-08:00", "training")])
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.memberships([trip("x", "2026-01-10T00:00:00")])

    def test_global_train_claim_outside_original_window_is_rejected(self):
        for departure in ("2025-12-31T23:59:59-08:00", "2026-06-24T00:00:00-07:00"):
            with self.subTest(departure=departure), self.assertRaisesRegex(ValueError, "outside M2"):
                self.memberships([trip("x", departure)])

    def test_accidentally_reassigned_heldouts_are_rejected(self):
        for fields in ({"vehicle_id": "VEHICLE-H"}, {"route_id": "ROUTE-H"},
                       {"unseen_vehicle": True}, {"unseen_route": True}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "heldout"):
                self.memberships([trip("x", "2026-01-10T00:00:00-08:00", **fields)])

    def test_fold_cannot_reach_protected_period_or_later_labels(self):
        for change in (
                {"validation_departure_dates": ["2026-06-01", "2026-07-01"]},
                {"validation_label_cutoff_at": "2026-07-01T00:00:01-07:00"}):
            folds = default_folds()
            folds[-1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_folds(folds, config())

    def test_overlapping_and_nonexpanding_folds_are_rejected(self):
        cases = []
        overlap = default_folds()
        overlap[1]["train_departure_dates"][1] = "2026-03-15"
        overlap[1]["training_cutoff_at"] = "2026-03-20T00:00:00-07:00"
        overlap[1]["validation_departure_dates"][0] = "2026-03-20"
        cases.append(overlap)
        nonexpanding = default_folds()
        nonexpanding[1]["train_departure_dates"][1] = "2026-02-21"
        cases.append(nonexpanding)
        for folds in cases:
            with self.subTest(folds=folds), self.assertRaises(ValueError):
                validate_folds(folds, config())

    def test_cutoffs_cannot_borrow_validation_information(self):
        folds = default_folds()[:1]
        folds[0]["training_cutoff_at"] = "2026-03-01T08:00:01Z"
        with self.assertRaisesRegex(ValueError, "Fit cutoff"):
            validate_folds(folds, config())

    def test_invalid_configuration_rejected_without_mutating_input(self):
        folds = default_folds()
        original = copy.deepcopy(folds)
        validate_folds(folds, config())
        self.assertEqual(folds, original)
        for bad in ([], [folds[0], folds[0]], [{"fold_id": "missing"}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_folds(bad, config())
        bad_config = config()
        bad_config["split"]["group_by"] = ["snapshot_id"]
        with self.assertRaisesRegex(ValueError, "grouping"):
            validate_folds(folds, bad_config)


if __name__ == "__main__":
    unittest.main()
