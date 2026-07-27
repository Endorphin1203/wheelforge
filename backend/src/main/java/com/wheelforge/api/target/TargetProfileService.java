package com.wheelforge.api.target;

import java.util.List;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class TargetProfileService {
  private final TargetProfileRepository repository;

  public TargetProfileService(TargetProfileRepository repository) {
    this.repository = repository;
  }

  @Transactional(readOnly = true)
  public List<TargetProfileView> enabledProfiles() {
    return repository.findAllByEnabledTrueOrderByOsAscArchitectureAscPythonVersionAsc().stream()
        .map(TargetProfileService::toView)
        .toList();
  }

  private static TargetProfileView toView(TargetProfileEntity profile) {
    return new TargetProfileView(
        UUID.fromString(profile.getId()),
        profile.getCode(),
        profile.getOs(),
        profile.getArchitecture(),
        profile.getPythonImplementation(),
        profile.getPythonVersion(),
        profile.getPythonFullVersion(),
        profile.getPlatformTag(),
        profile.getAbiTags(),
        profile.getValidationType(),
        profile.getValidationPolicyVersion());
  }

  public record TargetProfileView(
      UUID id,
      String code,
      String os,
      String architecture,
      String pythonImplementation,
      String pythonVersion,
      String pythonFullVersion,
      String platformTag,
      List<String> abiTags,
      String validationType,
      String validationPolicyVersion) {}
}
