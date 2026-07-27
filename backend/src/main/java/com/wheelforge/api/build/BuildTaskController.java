package com.wheelforge.api.build;

import com.wheelforge.api.security.CurrentUser;
import jakarta.validation.Valid;
import java.util.List;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/build-tasks")
public class BuildTaskController {
  private final BuildTaskService service;

  public BuildTaskController(BuildTaskService service) {
    this.service = service;
  }

  @PostMapping
  public ResponseEntity<BuildTaskService.BuildTaskView> create(
      @AuthenticationPrincipal CurrentUser currentUser,
      @Valid @RequestBody BuildTaskService.CreateBuildTaskRequest request) {
    return ResponseEntity.status(HttpStatus.CREATED)
        .body(service.create(currentUser.requireUserId(), request));
  }

  @GetMapping
  public List<BuildTaskService.BuildTaskView> list(
      @AuthenticationPrincipal CurrentUser currentUser) {
    return service.list(currentUser.requireUserId());
  }

  @GetMapping("/{id}")
  public BuildTaskService.BuildTaskView get(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return service.get(currentUser.requireUserId(), id);
  }

  @PostMapping("/{id}/cancel")
  public BuildTaskService.BuildTaskView cancel(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return service.cancel(currentUser.requireUserId(), id);
  }

  @PostMapping("/{id}/retry")
  public ResponseEntity<BuildTaskService.BuildTaskView> retry(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return ResponseEntity.status(HttpStatus.CREATED)
        .body(service.retry(currentUser.requireUserId(), id));
  }

  @DeleteMapping("/{id}")
  public ResponseEntity<Void> delete(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    service.delete(currentUser.requireUserId(), id);
    return ResponseEntity.noContent().build();
  }
}
