package com.wheelforge.api.security;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/auth")
public class AuthController {
  private final AuthService authService;

  public AuthController(AuthService authService) {
    this.authService = authService;
  }

  @PostMapping("/login")
  @ResponseStatus(HttpStatus.OK)
  public AuthService.TokenResponse login(@Valid @RequestBody LoginRequest request) {
    return authService.login(request.username(), request.password());
  }

  public record LoginRequest(@NotBlank String username, @NotBlank String password) {}
}
