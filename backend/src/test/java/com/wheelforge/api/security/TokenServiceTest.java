package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

class TokenServiceTest {
  private static final String TOKEN_SECRET = "0123456789abcdef0123456789abcdef";
  private static final Instant NOW = Instant.parse("2026-07-23T00:00:00Z");

  @Test
  void issuesHs256TokenWithThirtyMinuteExpiryAndCurrentUserClaims() {
    TokenService tokenService = tokenService(Clock.fixed(NOW, ZoneOffset.UTC));
    UserAccount account =
        new UserAccount(
            "d290f1ee-6c54-4b01-90e6-d701748f0851",
            "admin",
            "$argon2id$encoded",
            "ADMIN",
            "ACTIVE",
            LocalDateTime.ofInstant(NOW, ZoneOffset.UTC));

    TokenService.IssuedToken token = tokenService.issue(account);
    Optional<CurrentUser> currentUser = tokenService.parse(token.accessToken());

    assertThat(token.expiresAt()).isEqualTo(NOW.plusSeconds(30 * 60));
    assertThat(token.accessToken()).hasSizeGreaterThan(20).contains(".");
    assertThat(currentUser)
        .hasValueSatisfying(
            user -> {
              assertThat(user.requireUserId().toString()).isEqualTo(account.getId());
              assertThat(user.isAdmin()).isTrue();
            });
  }

  @Test
  void rejectsTamperedAndExpiredTokens() {
    TokenService issuingService = tokenService(Clock.fixed(NOW, ZoneOffset.UTC));
    UserAccount account =
        new UserAccount(
            "d290f1ee-6c54-4b01-90e6-d701748f0851",
            "alice",
            "$argon2id$encoded",
            "USER",
            "ACTIVE",
            LocalDateTime.ofInstant(NOW, ZoneOffset.UTC));
    String token = issuingService.issue(account).accessToken();

    assertThat(issuingService.parse(token + "x")).isEmpty();
    assertThat(tokenService(Clock.fixed(NOW.plusSeconds(30 * 60), ZoneOffset.UTC)).parse(token))
        .isEmpty();
  }

  @Test
  void refusesSecretsShorterThanThirtyTwoUtf8Bytes() {
    assertThatThrownBy(() -> new TokenService("too-short", new ObjectMapper(), Clock.systemUTC()))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("at least 32 UTF-8 bytes");
  }

  private TokenService tokenService(Clock clock) {
    return new TokenService(TOKEN_SECRET, new ObjectMapper(), clock);
  }
}
