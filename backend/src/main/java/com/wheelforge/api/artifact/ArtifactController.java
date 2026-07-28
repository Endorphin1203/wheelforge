package com.wheelforge.api.artifact;

import com.wheelforge.api.security.CurrentUser;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.UUID;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

@RestController
@RequestMapping("/api/artifacts")
public class ArtifactController {
  private final ArtifactService service;

  public ArtifactController(ArtifactService service) {
    this.service = service;
  }

  @GetMapping
  public List<ArtifactService.ArtifactView> list(
      @AuthenticationPrincipal CurrentUser currentUser,
      @RequestParam(required = false) java.time.LocalDateTime beforeCreatedAt,
      @RequestParam(required = false) UUID beforeId,
      @RequestParam(defaultValue = "50") int limit) {
    return service.list(currentUser.requireUserId(), beforeCreatedAt, beforeId, limit);
  }

  @GetMapping("/{id}")
  public ArtifactService.ArtifactView get(
      @AuthenticationPrincipal CurrentUser currentUser, @PathVariable UUID id) {
    return service.get(currentUser.requireUserId(), id);
  }

  @GetMapping("/{id}/download")
  public ResponseEntity<StreamingResponseBody> download(
      @AuthenticationPrincipal CurrentUser currentUser,
      @PathVariable UUID id,
      HttpServletRequest request) {
    ArtifactService.DownloadTicket ticket =
        service.prepareDownload(
            currentUser.requireUserId(),
            id,
            request.getRemoteAddr(),
            request.getHeader(HttpHeaders.USER_AGENT));
    StreamingResponseBody body = output -> service.stream(ticket, output);
    return ResponseEntity.ok()
        .contentType(MediaType.parseMediaType("application/zip"))
        .contentLength(ticket.sizeBytes())
        .header(
            HttpHeaders.CONTENT_DISPOSITION,
            ContentDisposition.attachment().filename("wheelhouse.zip").build().toString())
        .body(body);
  }
}
