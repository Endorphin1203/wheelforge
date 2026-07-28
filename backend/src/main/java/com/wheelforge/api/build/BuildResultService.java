package com.wheelforge.api.build;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.requirements.RequirementItemEntity;
import com.wheelforge.api.requirements.RequirementItemRepository;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;

@Service
public class BuildResultService {
  static final int MAX_LOG_ROWS = 500;

  private final BuildTaskRepository taskRepository;
  private final BuildLogRepository logRepository;
  private final ResolvedPackageRepository resolvedRepository;
  private final RequirementItemRepository itemRepository;

  public BuildResultService(
      BuildTaskRepository taskRepository,
      BuildLogRepository logRepository,
      ResolvedPackageRepository resolvedRepository,
      RequirementItemRepository itemRepository) {
    this.taskRepository = taskRepository;
    this.logRepository = logRepository;
    this.resolvedRepository = resolvedRepository;
    this.itemRepository = itemRepository;
  }

  @Transactional(readOnly = true)
  public List<BuildLogView> logs(UUID userId, UUID taskId, long afterSequence) {
    if (afterSequence < 0) {
      throw ApiException.badRequest("INVALID_LOG_CURSOR", "afterSequence must be non-negative");
    }
    activeOwnedTask(userId, taskId);
    return logRepository
        .findByBuildTaskIdAndSequenceNoGreaterThanOrderBySequenceNoAsc(
            taskId.toString(), afterSequence, PageRequest.of(0, MAX_LOG_ROWS))
        .stream()
        .map(BuildResultService::logView)
        .toList();
  }

  @Transactional(readOnly = true)
  public List<ResolvedPackageView> resolvedPackages(UUID userId, UUID taskId) {
    activeOwnedTask(userId, taskId);
    return resolvedRepository
        .findAllByBuildTaskIdOrderByNormalizedNameAsc(taskId.toString())
        .stream()
        .map(BuildResultService::resolvedView)
        .toList();
  }

  @Transactional(readOnly = true)
  public List<VersionComparisonRow> versionComparison(UUID userId, UUID taskId) {
    BuildTaskEntity task = activeOwnedTask(userId, taskId);
    LinkedHashMap<String, RequirementItemEntity> directItems = new LinkedHashMap<>();
    itemRepository.findAllByRequirementFileIdOrderByLineNoAsc(task.getRequirementFileId()).stream()
        .filter(RequirementItemEntity::isSupported)
        .forEach(item -> directItems.putIfAbsent(item.getNormalizedName(), item));

    List<ResolvedPackageEntity> resolvedPackages =
        resolvedRepository.findAllByBuildTaskIdOrderByNormalizedNameAsc(taskId.toString());
    Map<String, ResolvedPackageEntity> resolvedByName =
        resolvedPackages.stream()
            .collect(
                Collectors.toMap(
                    ResolvedPackageEntity::getNormalizedName,
                    Function.identity(),
                    (first, ignored) -> first));

    List<VersionComparisonRow> rows = new ArrayList<>();
    directItems.forEach(
        (name, item) -> {
          ResolvedPackageEntity resolved = resolvedByName.get(name);
          rows.add(resolved == null ? unresolvedDirect(item) : directComparison(item, resolved));
        });
    resolvedPackages.stream()
        .filter(resolved -> !directItems.containsKey(resolved.getNormalizedName()))
        .sorted(Comparator.comparing(ResolvedPackageEntity::getNormalizedName))
        .map(BuildResultService::transitiveComparison)
        .forEach(rows::add);
    return List.copyOf(rows);
  }

  private BuildTaskEntity activeOwnedTask(UUID userId, UUID taskId) {
    return taskRepository
        .findByIdAndUserIdAndDeletedAtIsNull(taskId.toString(), userId.toString())
        .orElseThrow(() -> ApiException.notFound("Build task was not found"));
  }

  private static BuildLogView logView(BuildLogEntity log) {
    return new BuildLogView(
        log.getSequenceNo(),
        log.getStage(),
        log.getLevel(),
        log.getMessage(),
        log.getContext(),
        log.getCreatedAt());
  }

  private static ResolvedPackageView resolvedView(ResolvedPackageEntity resolved) {
    return new ResolvedPackageView(
        resolved.getNormalizedName(),
        resolved.getFinalVersion(),
        resolved.getDependencyType(),
        resolved.getOriginalConstraint(),
        resolved.getStrictVersion(),
        resolved.getChangeDirection(),
        resolved.getChangeReason(),
        resolved.getCandidateAttempts(),
        resolved.getWheelFilename(),
        resolved.getWheelTags(),
        resolved.getPackageSource(),
        resolved.getSha256(),
        resolved.getWheelStatus(),
        resolved.getErrorMessage());
  }

  private static VersionComparisonRow directComparison(
      RequirementItemEntity item, ResolvedPackageEntity resolved) {
    return new VersionComparisonRow(
        item.getNormalizedName(),
        "DIRECT",
        item.getSpecifier(),
        resolved.getStrictVersion(),
        resolved.getFinalVersion(),
        resolved.getChangeDirection(),
        resolved.getChangeReason(),
        resolved.getWheelStatus(),
        resolved.getPackageSource());
  }

  private static VersionComparisonRow unresolvedDirect(RequirementItemEntity item) {
    return new VersionComparisonRow(
        item.getNormalizedName(),
        "DIRECT",
        item.getSpecifier(),
        null,
        null,
        "UNRESOLVED",
        "No compatible Wheel was resolved",
        "MISSING",
        null);
  }

  private static VersionComparisonRow transitiveComparison(ResolvedPackageEntity resolved) {
    return new VersionComparisonRow(
        resolved.getNormalizedName(),
        "TRANSITIVE",
        resolved.getOriginalConstraint(),
        resolved.getStrictVersion(),
        resolved.getFinalVersion(),
        resolved.getChangeDirection(),
        resolved.getChangeReason(),
        resolved.getWheelStatus(),
        resolved.getPackageSource());
  }

  public record BuildLogView(
      long sequence,
      String stage,
      String level,
      String message,
      JsonNode context,
      LocalDateTime createdAt) {}

  public record ResolvedPackageView(
      String normalizedName,
      String finalVersion,
      String dependencyType,
      String originalConstraint,
      String strictVersion,
      String changeDirection,
      String changeReason,
      JsonNode candidateAttempts,
      String wheelFilename,
      List<String> wheelTags,
      String packageSource,
      String sha256,
      String wheelStatus,
      String errorMessage) {
    public ResolvedPackageView {
      wheelTags = wheelTags == null ? null : List.copyOf(wheelTags);
    }
  }
}
