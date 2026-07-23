package com.wheelforge.api.security;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Optional;
import javax.crypto.Mac;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import tools.jackson.databind.ObjectMapper;

@Service
public class TokenService {
  private static final String HEADER = "{\"alg\":\"HS256\",\"typ\":\"JWT\"}";
  private static final Duration TOKEN_TTL = Duration.ofMinutes(30);
  private static final Base64.Encoder BASE64_URL_ENCODER = Base64.getUrlEncoder().withoutPadding();
  private static final Base64.Decoder BASE64_URL_DECODER = Base64.getUrlDecoder();

  private final SecretKey signingKey;
  private final ObjectMapper objectMapper;
  private final Clock clock;

  @Autowired
  public TokenService(
      @Value("${wheelforge.auth.token-secret}") String tokenSecret, ObjectMapper objectMapper) {
    this(tokenSecret, objectMapper, Clock.systemUTC());
  }

  TokenService(String tokenSecret, ObjectMapper objectMapper, Clock clock) {
    byte[] secretBytes = tokenSecret.getBytes(StandardCharsets.UTF_8);
    if (secretBytes.length < 32) {
      throw new IllegalStateException("WF_AUTH_TOKEN_SECRET must be at least 32 UTF-8 bytes");
    }
    this.signingKey = new SecretKeySpec(secretBytes, "HmacSHA256");
    this.objectMapper = objectMapper;
    this.clock = clock;
  }

  public IssuedToken issue(UserAccount account) {
    Instant issuedAt = clock.instant();
    Instant expiresAt = issuedAt.plus(TOKEN_TTL);
    TokenClaims claims =
        new TokenClaims(
            account.getId(),
            account.getRole(),
            issuedAt.getEpochSecond(),
            expiresAt.getEpochSecond());
    String header = BASE64_URL_ENCODER.encodeToString(HEADER.getBytes(StandardCharsets.UTF_8));
    String payload = BASE64_URL_ENCODER.encodeToString(writeClaims(claims));
    String signature = BASE64_URL_ENCODER.encodeToString(sign(header + "." + payload));
    return new IssuedToken(header + "." + payload + "." + signature, expiresAt);
  }

  public Optional<CurrentUser> parse(String token) {
    if (token == null || token.isBlank()) {
      return Optional.empty();
    }
    try {
      String[] segments = token.split("\\.", -1);
      if (segments.length != 3
          || !HEADER.equals(
              new String(BASE64_URL_DECODER.decode(segments[0]), StandardCharsets.UTF_8))) {
        return Optional.empty();
      }
      byte[] expectedSignature = sign(segments[0] + "." + segments[1]);
      byte[] actualSignature = BASE64_URL_DECODER.decode(segments[2]);
      if (!MessageDigest.isEqual(expectedSignature, actualSignature)) {
        return Optional.empty();
      }
      TokenClaims claims =
          objectMapper.readValue(BASE64_URL_DECODER.decode(segments[1]), TokenClaims.class);
      if (claims.exp() <= clock.instant().getEpochSecond()) {
        return Optional.empty();
      }
      return Optional.of(new CurrentUser(java.util.UUID.fromString(claims.sub()), claims.role()));
    } catch (Exception exception) {
      return Optional.empty();
    }
  }

  private byte[] writeClaims(TokenClaims claims) {
    try {
      return objectMapper.writeValueAsBytes(claims);
    } catch (Exception exception) {
      throw new IllegalStateException("Could not create authentication token", exception);
    }
  }

  private byte[] sign(String value) {
    try {
      Mac mac = Mac.getInstance("HmacSHA256");
      mac.init(signingKey);
      return mac.doFinal(value.getBytes(StandardCharsets.US_ASCII));
    } catch (GeneralSecurityException exception) {
      throw new IllegalStateException("Could not sign authentication token", exception);
    }
  }

  public record IssuedToken(String accessToken, Instant expiresAt) {}

  private record TokenClaims(String sub, String role, long iat, long exp) {}
}
