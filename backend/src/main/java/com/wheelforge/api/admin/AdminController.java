package com.wheelforge.api.admin;

import com.wheelforge.api.security.CurrentUser;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/admin")
public class AdminController {
  private final AdminService service;

  public AdminController(AdminService service) {
    this.service = service;
  }

  @PreAuthorize("hasRole('ADMIN')")
  @GetMapping("/users")
  public List<AdminService.UserView> users() {
    return service.users();
  }

  @PreAuthorize("hasRole('ADMIN')")
  @PostMapping("/users")
  public ResponseEntity<AdminService.UserView> createUser(
      @AuthenticationPrincipal CurrentUser currentUser,
      @RequestBody AdminService.CreateUserRequest request) {
    return ResponseEntity.status(HttpStatus.CREATED)
        .body(service.createUser(currentUser.requireUserId(), request));
  }

  @PreAuthorize("hasRole('ADMIN')")
  @PutMapping("/users/{id}/status")
  public AdminService.UserView updateUserStatus(
      @AuthenticationPrincipal CurrentUser currentUser,
      @PathVariable UUID id,
      @RequestBody AdminService.UpdateUserStatusRequest request) {
    return service.updateUserStatus(currentUser.requireUserId(), id, request);
  }

  @PreAuthorize("hasRole('ADMIN')")
  @GetMapping("/package-sources")
  public List<AdminService.PackageSourceView> packageSources() {
    return service.packageSources();
  }

  @PreAuthorize("hasRole('ADMIN')")
  @PutMapping("/package-sources/{id}")
  public AdminService.PackageSourceView updatePackageSource(
      @PathVariable UUID id, @RequestBody AdminService.PackageSourceUpdate request) {
    return service.updatePackageSource(id, request);
  }

  @PreAuthorize("hasRole('ADMIN')")
  @GetMapping("/system-config")
  public List<AdminService.SystemConfigView> systemConfig() {
    return service.systemConfig();
  }

  @PreAuthorize("hasRole('ADMIN')")
  @PutMapping("/system-config")
  public List<AdminService.SystemConfigView> updateSystemConfig(
      @AuthenticationPrincipal CurrentUser currentUser,
      @RequestBody Map<String, AdminService.ConfigUpdate> updates) {
    return service.updateSystemConfig(currentUser.requireUserId(), updates);
  }
}
