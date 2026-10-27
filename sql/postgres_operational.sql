CREATE SCHEMA "ds_m01_m1";

CREATE TABLE "ds_m01_m1"."core_tenant" (
  "tenant_id" TEXT NOT NULL,
  "tenant_name" TEXT NOT NULL,
  "report_timezone" TEXT NOT NULL,
  "base_currency" TEXT NOT NULL,
  PRIMARY KEY ("tenant_id")
);

CREATE TABLE "ds_m01_m1"."core_vehicle" (
  "tenant_id" TEXT NOT NULL,
  "vehicle_id" TEXT NOT NULL,
  "vin_raw" TEXT,
  "vin" TEXT,
  "vehicle_class" TEXT NOT NULL,
  "fuel_type" TEXT NOT NULL,
  PRIMARY KEY ("tenant_id", "vehicle_id")
);

CREATE TABLE "ds_m01_m1"."core_device" (
  "tenant_id" TEXT NOT NULL,
  "device_id" TEXT NOT NULL,
  "device_model" TEXT NOT NULL,
  PRIMARY KEY ("tenant_id", "device_id")
);

CREATE TABLE "ds_m01_m1"."core_vehicle_device_assignment" (
  "tenant_id" TEXT NOT NULL,
  "assignment_id" TEXT NOT NULL,
  "vehicle_id" TEXT NOT NULL,
  "device_id" TEXT NOT NULL,
  "valid_from" TIMESTAMPTZ NOT NULL,
  "valid_to" TIMESTAMPTZ,
  "known_from" TIMESTAMPTZ NOT NULL,
  "known_to" TIMESTAMPTZ,
  "valid_to_state" TEXT NOT NULL,
  "known_to_state" TEXT NOT NULL,
  "resolution_status" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  PRIMARY KEY ("tenant_id", "assignment_id")
);

CREATE TABLE "ds_m01_m1"."eta_trip" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "trip_id" TEXT NOT NULL,
  "registered_at" TIMESTAMPTZ NOT NULL,
  PRIMARY KEY ("tenant_id", "trip_id")
);

CREATE TABLE "ds_m01_m1"."eta_facility_version" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "facility_version_id" TEXT NOT NULL,
  "facility_id" TEXT NOT NULL,
  "timezone" TEXT NOT NULL,
  "arrival_definition" TEXT NOT NULL,
  "latitude_deg" DOUBLE PRECISION NOT NULL,
  "longitude_deg" DOUBLE PRECISION NOT NULL,
  "valid_from" TIMESTAMPTZ NOT NULL,
  "valid_to" TIMESTAMPTZ,
  "known_from" TIMESTAMPTZ NOT NULL,
  "known_to" TIMESTAMPTZ,
  PRIMARY KEY ("tenant_id", "facility_version_id")
);

CREATE TABLE "ds_m01_m1"."eta_route_version" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "route_version_id" TEXT NOT NULL,
  "route_id" TEXT NOT NULL,
  "origin_facility_version_id" TEXT NOT NULL,
  "destination_facility_version_id" TEXT NOT NULL,
  "planned_distance_km" DECIMAL(12,3) NOT NULL,
  "valid_from" TIMESTAMPTZ NOT NULL,
  "valid_to" TIMESTAMPTZ,
  "known_at" TIMESTAMPTZ NOT NULL,
  PRIMARY KEY ("tenant_id", "route_version_id")
);

CREATE TABLE "ds_m01_m1"."eta_trip_plan_version" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "trip_version_id" TEXT NOT NULL,
  "trip_id" TEXT NOT NULL,
  "vehicle_id" TEXT NOT NULL,
  "route_version_id" TEXT NOT NULL,
  "planned_departure_at" TIMESTAMPTZ NOT NULL,
  "origin_facility_version_id" TEXT NOT NULL,
  "destination_facility_version_id" TEXT NOT NULL,
  "plan_known_at" TIMESTAMPTZ NOT NULL,
  "supersedes_trip_version_id" TEXT,
  PRIMARY KEY ("tenant_id", "trip_version_id")
);

CREATE TABLE "ds_m01_m1"."eta_promise_version" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "promise_version_id" TEXT NOT NULL,
  "trip_id" TEXT NOT NULL,
  "window_start_at" TIMESTAMPTZ NOT NULL,
  "window_end_at" TIMESTAMPTZ NOT NULL,
  "known_at" TIMESTAMPTZ NOT NULL,
  "supersedes_promise_version_id" TEXT,
  "change_reason" TEXT NOT NULL,
  PRIMARY KEY ("tenant_id", "promise_version_id")
);

CREATE TABLE "ds_m01_m1"."eta_trip_event" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "trip_event_id" TEXT NOT NULL,
  "trip_id" TEXT NOT NULL,
  "event_type" TEXT NOT NULL,
  "occurred_at" TIMESTAMPTZ NOT NULL,
  "available_at" TIMESTAMPTZ NOT NULL,
  "trip_status" TEXT NOT NULL,
  PRIMARY KEY ("tenant_id", "trip_event_id")
);

CREATE TABLE "ds_m01_m1"."eta_trip_outcome" (
  "tenant_id" TEXT NOT NULL,
  "provenance_id" TEXT NOT NULL,
  "outcome_version_id" TEXT NOT NULL,
  "trip_id" TEXT NOT NULL,
  "actual_arrival_time" TIMESTAMPTZ,
  "service_start_time" TIMESTAMPTZ,
  "delivery_completed_time" TIMESTAMPTZ,
  "cancellation_time" TIMESTAMPTZ,
  "label_available_time" TIMESTAMPTZ NOT NULL,
  "label_status" TEXT NOT NULL,
  "arrival_evidence_ref" TEXT,
  "arrival_time_precision" TEXT NOT NULL,
  "reviewer_id" TEXT,
  "cancellation_reason" TEXT,
  "followup_through" TIMESTAMPTZ NOT NULL,
  "followup_end_reason" TEXT NOT NULL,
  "revision_reason" TEXT,
  "supersedes_outcome_version_id" TEXT,
  PRIMARY KEY ("tenant_id", "outcome_version_id")
);
