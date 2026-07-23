package com.wheelforge.api.security;

import java.util.UUID;

public record CurrentUser(UUID userId, String role) {
  public CurrentUser {
    if (userId == null) {
      throw new IllegalArgumentException("Token subject is required");
    }
    if (!"USER".equals(role) && !"ADMIN".equals(role)) {
      throw new IllegalArgumentException("Token role is invalid");
    }
  }

  public UUID requireUserId() {
    return userId;
  }

  public boolean isAdmin() {
    return "ADMIN".equals(role);
  }
}
