package com.wheelforge.api.build;

import com.wheelforge.api.common.ApiException;
import java.util.EnumMap;
import java.util.EnumSet;
import java.util.Map;

public final class BuildStateMachine {
  private static final Map<BuildStatus, EnumSet<BuildStatus>> ALLOWED = allowedTransitions();

  public boolean isAllowed(BuildStatus from, BuildStatus to) {
    return ALLOWED.get(from).contains(to);
  }

  public void requireTransition(BuildStatus from, BuildStatus to) {
    if (!isAllowed(from, to)) {
      throw ApiException.conflict(
          "INVALID_BUILD_TRANSITION", "Build task transition is not allowed");
    }
  }

  private static Map<BuildStatus, EnumSet<BuildStatus>> allowedTransitions() {
    var transitions = new EnumMap<BuildStatus, EnumSet<BuildStatus>>(BuildStatus.class);
    transitions.put(
        BuildStatus.CREATED,
        EnumSet.of(
            BuildStatus.PARSING, BuildStatus.QUEUED, BuildStatus.CANCELLED, BuildStatus.FAILED));
    transitions.put(
        BuildStatus.PARSING,
        EnumSet.of(BuildStatus.QUEUED, BuildStatus.CANCELLED, BuildStatus.FAILED));
    transitions.put(
        BuildStatus.QUEUED,
        EnumSet.of(BuildStatus.RESOLVING, BuildStatus.CANCELLED, BuildStatus.FAILED));
    transitions.put(
        BuildStatus.RESOLVING,
        EnumSet.of(BuildStatus.DOWNLOADING, BuildStatus.CANCELLED, BuildStatus.FAILED));
    transitions.put(
        BuildStatus.DOWNLOADING,
        EnumSet.of(BuildStatus.VALIDATING, BuildStatus.CANCELLED, BuildStatus.FAILED));
    transitions.put(
        BuildStatus.VALIDATING,
        EnumSet.of(
            BuildStatus.PACKAGING,
            BuildStatus.PARTIAL_SUCCESS,
            BuildStatus.CANCELLED,
            BuildStatus.FAILED));
    transitions.put(
        BuildStatus.PACKAGING,
        EnumSet.of(
            BuildStatus.SUCCESS,
            BuildStatus.PARTIAL_SUCCESS,
            BuildStatus.CANCELLED,
            BuildStatus.FAILED));
    for (BuildStatus terminal :
        EnumSet.of(
            BuildStatus.SUCCESS,
            BuildStatus.PARTIAL_SUCCESS,
            BuildStatus.FAILED,
            BuildStatus.CANCELLED)) {
      transitions.put(terminal, EnumSet.noneOf(BuildStatus.class));
    }
    return Map.copyOf(transitions);
  }
}
