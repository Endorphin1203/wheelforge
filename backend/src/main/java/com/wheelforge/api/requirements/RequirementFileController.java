package com.wheelforge.api.requirements;

import com.wheelforge.api.security.CurrentUser;
import java.util.List;
import java.util.UUID;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api/requirement-files")
public class RequirementFileController {
  private final RequirementFileService service;

  public RequirementFileController(RequirementFileService service) {
    this.service = service;
  }

  @PostMapping(consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
  public ResponseEntity<RequirementFileService.RequirementFileView> upload(
      @AuthenticationPrincipal CurrentUser currentUser, @RequestPart("file") MultipartFile file) {
    return ResponseEntity.accepted().body(service.upload(currentUser.requireUserId(), file));
  }

  @GetMapping("/{id}")
  public RequirementFileService.RequirementFileView get(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return service.get(currentUser.requireUserId(), id);
  }

  @GetMapping("/{id}/items")
  public List<RequirementFileService.RequirementItemView> items(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return service.items(currentUser.requireUserId(), id);
  }
}
