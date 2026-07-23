package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;

@ExtendWith(MockitoExtension.class)
class AuthServiceTest {
  @Mock private UserAccountRepository userAccountRepository;
  @Mock private PasswordEncoder passwordEncoder;
  @Mock private TokenService tokenService;
  @InjectMocks private AuthService authService;

  @Test
  void returnsTokenForActiveAccountWithMatchingPassword() {
    UserAccount account =
        new UserAccount(
            "d290f1ee-6c54-4b01-90e6-d701748f0851",
            "alice",
            "$argon2id$encoded",
            "USER",
            "ACTIVE",
            LocalDateTime.ofInstant(Instant.parse("2026-07-23T00:00:00Z"), ZoneOffset.UTC));
    TokenService.IssuedToken issuedToken =
        new TokenService.IssuedToken("signed-token", Instant.parse("2026-07-23T00:30:00Z"));
    given(userAccountRepository.findByUsername("alice")).willReturn(Optional.of(account));
    given(passwordEncoder.matches("correct horse battery staple", account.getPasswordHash()))
        .willReturn(true);
    given(tokenService.issue(account)).willReturn(issuedToken);

    AuthService.TokenResponse response = authService.login("alice", "correct horse battery staple");

    assertThat(response.accessToken()).isEqualTo("signed-token");
    assertThat(response.expiresAt()).isEqualTo(issuedToken.expiresAt());
  }

  @Test
  void rejectsWrongPasswordWithTheGenericError() {
    UserAccount account =
        new UserAccount(
            "d290f1ee-6c54-4b01-90e6-d701748f0851",
            "alice",
            "$argon2id$encoded",
            "USER",
            "ACTIVE",
            LocalDateTime.now(ZoneOffset.UTC));
    given(userAccountRepository.findByUsername("alice")).willReturn(Optional.of(account));
    given(passwordEncoder.matches("wrong", account.getPasswordHash())).willReturn(false);

    assertThatThrownBy(() -> authService.login("alice", "wrong"))
        .isInstanceOf(AuthService.AuthenticationFailedException.class)
        .hasMessage("Invalid username or password");

    verify(passwordEncoder).matches("wrong", "$argon2id$encoded");
  }

  @Test
  void verifiesUnknownUsersAgainstTheDummyHashAndNeverIssuesAToken() {
    given(userAccountRepository.findByUsername("missing")).willReturn(Optional.empty());
    given(passwordEncoder.matches("wrong", AuthService.DUMMY_PASSWORD_HASH)).willReturn(false);

    assertThatThrownBy(() -> authService.login("missing", "wrong"))
        .isInstanceOf(AuthService.AuthenticationFailedException.class)
        .hasMessage("Invalid username or password");

    verify(passwordEncoder).matches("wrong", AuthService.DUMMY_PASSWORD_HASH);
    verify(tokenService, never()).issue(org.mockito.ArgumentMatchers.any());
  }

  @Test
  void dummyHashIsValidArgon2idAndDoesNotMatchAnArbitraryPassword() {
    PasswordEncoder realEncoder = Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();

    assertThat(realEncoder.matches("wrong", AuthService.DUMMY_PASSWORD_HASH)).isFalse();
  }

  @Test
  void rejectsInactiveAccountsWithTheGenericError() {
    UserAccount account =
        new UserAccount(
            "d290f1ee-6c54-4b01-90e6-d701748f0851",
            "alice",
            "$argon2id$encoded",
            "USER",
            "DISABLED",
            LocalDateTime.now(ZoneOffset.UTC));
    given(userAccountRepository.findByUsername("alice")).willReturn(Optional.of(account));
    given(passwordEncoder.matches("wrong", account.getPasswordHash())).willReturn(true);

    assertThatThrownBy(() -> authService.login("alice", "wrong"))
        .isInstanceOf(AuthService.AuthenticationFailedException.class)
        .hasMessage("Invalid username or password");
  }
}
