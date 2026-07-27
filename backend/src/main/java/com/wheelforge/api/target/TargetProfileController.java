package com.wheelforge.api.target;

import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/target-profiles")
public class TargetProfileController {
  private final TargetProfileService service;

  public TargetProfileController(TargetProfileService service) {
    this.service = service;
  }

  @GetMapping
  public List<TargetProfileService.TargetProfileView> enabledProfiles() {
    return service.enabledProfiles();
  }
}
