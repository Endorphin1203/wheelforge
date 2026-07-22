package com.wheelforge.api.build;

public enum BuildStatus {
    CREATED,
    PARSING,
    QUEUED,
    RESOLVING,
    DOWNLOADING,
    VALIDATING,
    PACKAGING,
    SUCCESS,
    PARTIAL_SUCCESS,
    FAILED,
    CANCELLED
}
