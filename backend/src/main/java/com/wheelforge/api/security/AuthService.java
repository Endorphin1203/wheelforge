package com.wheelforge.api.security;

import java.time.Instant;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

@Service
public class AuthService {
  static final String DUMMY_PASSWORD_HASH =
      "$argon2id$v=19$m=16384,t=2,p=1$c2FsdDEyMzQ1Njc4OTAxMg$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

  private final UserAccountRepository userAccountRepository;
  private final PasswordEncoder passwordEncoder;
  private final TokenService tokenService;

  public AuthService(
      UserAccountRepository userAccountRepository,
      PasswordEncoder passwordEncoder,
      TokenService tokenService) {
    this.userAccountRepository = userAccountRepository;
    this.passwordEncoder = passwordEncoder;
    this.tokenService = tokenService;
  }

  public TokenResponse login(String username, String password) {
    UserAccount account = userAccountRepository.findByUsername(username).orElse(null);
    String passwordHash = account == null ? DUMMY_PASSWORD_HASH : account.getPasswordHash();
    boolean passwordMatches = passwordEncoder.matches(password, passwordHash);
    if (account == null || !passwordMatches || !account.isActive()) {
      throw new AuthenticationFailedException();
    }
    TokenService.IssuedToken token = tokenService.issue(account);
    return new TokenResponse(token.accessToken(), token.expiresAt());
  }

  public record TokenResponse(String accessToken, Instant expiresAt) {}

  public static class AuthenticationFailedException extends RuntimeException {
    public AuthenticationFailedException() {
      super("Invalid username or password");
    }
  }
}
