package com.wheelforge.api.build;

public record VersionComparisonRow(
    String packageName,
    String dependencyType,
    String originalConstraint,
    String strictVersion,
    String finalVersion,
    String changeDirection,
    String changeReason,
    String wheelStatus,
    String packageSource) {}
