package com.wheelforge.api.build;

import com.wheelforge.api.security.CurrentUser;
import java.util.List;
import java.util.UUID;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/build-tasks/{id}")
public class BuildResultController {
  public static final String VALIDATION_HEADER = "X-WheelForge-Validation";
  private static final String STATIC_VALIDATION = "STATIC";

  private final BuildResultService service;

  public BuildResultController(BuildResultService service) {
    this.service = service;
  }

  @GetMapping("/logs")
  public ResponseEntity<List<BuildResultService.BuildLogView>> logs(
      @AuthenticationPrincipal CurrentUser currentUser,
      @PathVariable UUID id,
      @RequestParam(defaultValue = "0") long afterSequence) {
    return staticResult(service.logs(currentUser.requireUserId(), id, afterSequence));
  }

  @GetMapping("/resolved-packages")
  public ResponseEntity<List<BuildResultService.ResolvedPackageView>> resolvedPackages(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return staticResult(service.resolvedPackages(currentUser.requireUserId(), id));
  }

  @GetMapping("/version-comparison")
  public ResponseEntity<List<VersionComparisonRow>> versionComparison(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return staticResult(service.versionComparison(currentUser.requireUserId(), id));
  }

  private static <T> ResponseEntity<T> staticResult(T body) {
    return ResponseEntity.ok().header(VALIDATION_HEADER, STATIC_VALIDATION).body(body);
  }
}
