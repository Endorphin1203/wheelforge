package com.wheelforge.api.admin;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.BDDMockito.given;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import java.security.SecureRandom;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import tools.jackson.databind.ObjectMapper;

@ExtendWith(MockitoExtension.class)
class AdminServiceTest {
  private static final UUID ADMIN_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID USER_ID = UUID.fromString("fe3b9a09-e696-4104-beb7-d8fd1fb85d24");
  private static final UUID SOURCE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final Instant NOW = Instant.parse("2026-07-28T01:02:03Z");

  @Mock private UserAccountRepository userRepository;
  @Mock private PackageSourceRepository sourceRepository;
  @Mock private SystemConfigRepository configRepository;
  private final ObjectMapper objectMapper = new ObjectMapper();
  private final Argon2PasswordEncoder passwordEncoder =
      Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();
  private AdminService service;

  @BeforeEach
  void setUp() {
    service =
        new AdminService(
            userRepository,
            sourceRepository,
            configRepository,
            passwordEncoder,
            objectMapper,
            new SecureRandom(),
            Clock.fixed(NOW, ZoneOffset.UTC));
  }

  @Test
  void createsUniqueUsersWithArgon2AndReturnsOnlyGeneratedPasswords() {
    given(userRepository.findByUsername("alice")).willReturn(Optional.empty());

    AdminService.UserView generated =
        service.createUser(ADMIN_ID, new AdminService.CreateUserRequest("alice", "USER", null));

    ArgumentCaptor<UserAccount> saved = ArgumentCaptor.forClass(UserAccount.class);
    org.mockito.Mockito.verify(userRepository).saveAndFlush(saved.capture());
    assertThat(saved.getValue().getPasswordHash()).startsWith("$argon2id$");
    assertThat(
            passwordEncoder.matches(
                generated.initialPassword(), saved.getValue().getPasswordHash()))
        .isTrue();
    assertThat(generated.initialPassword()).hasSizeGreaterThanOrEqualTo(20);

    given(userRepository.findByUsername("bob")).willReturn(Optional.empty());
    AdminService.UserView supplied =
        service.createUser(
            ADMIN_ID, new AdminService.CreateUserRequest("bob", "ADMIN", "Strong-password-123"));
    assertThat(supplied.initialPassword()).isNull();
  }

  @Test
  void rejectsDuplicateUsernameInvalidRoleWeakPasswordAndSelfDisable() {
    given(userRepository.findByUsername("alice"))
        .willReturn(Optional.of(user(USER_ID, "alice", "USER", "ACTIVE")));
    assertApiError(
        () ->
            service.createUser(
                ADMIN_ID,
                new AdminService.CreateUserRequest("alice", "USER", "Strong-password-123")),
        409,
        "USERNAME_EXISTS");
    assertApiError(
        () ->
            service.createUser(
                ADMIN_ID,
                new AdminService.CreateUserRequest("new", "AUDITOR", "Strong-password-123")),
        400,
        "INVALID_USER_ROLE");
    assertApiError(
        () ->
            service.createUser(
                ADMIN_ID, new AdminService.CreateUserRequest("new", "USER", "short")),
        400,
        "INVALID_INITIAL_PASSWORD");

    assertApiError(
        () ->
            service.updateUserStatus(
                ADMIN_ID, ADMIN_ID, new AdminService.UpdateUserStatusRequest("DISABLED")),
        409,
        "CANNOT_DISABLE_SELF");
  }

  @Test
  void administratorCanDisableAnotherUser() {
    UserAccount target = user(USER_ID, "alice", "USER", "ACTIVE");
    given(userRepository.findById(USER_ID.toString())).willReturn(Optional.of(target));

    AdminService.UserView updated =
        service.updateUserStatus(
            ADMIN_ID, USER_ID, new AdminService.UpdateUserStatusRequest("DISABLED"));

    assertThat(updated.status()).isEqualTo("DISABLED");
    assertThat(target.isActive()).isFalse();
  }

  @Test
  void updatesOnlyBuiltInSourceOperationalFieldsWithinBounds() {
    PackageSourceEntity source = source("TSINGHUA");
    given(sourceRepository.findById(SOURCE_ID.toString())).willReturn(Optional.of(source));

    AdminService.PackageSourceView updated =
        service.updatePackageSource(
            SOURCE_ID, new AdminService.PackageSourceUpdate(false, 20, 45, null, null));
    assertThat(updated.enabled()).isFalse();
    assertThat(updated.priorityNo()).isEqualTo(20);
    assertThat(updated.timeoutSeconds()).isEqualTo(45);
    assertThat(updated.baseUrl()).isEqualTo("https://pypi.tuna.tsinghua.edu.cn/simple");

    assertApiError(
        () ->
            service.updatePackageSource(
                SOURCE_ID,
                new AdminService.PackageSourceUpdate(
                    true, 10, 30, null, "https://evil.example/simple")),
        400,
        "IMMUTABLE_SOURCE_FIELD");
    assertApiError(
        () ->
            service.updatePackageSource(
                SOURCE_ID, new AdminService.PackageSourceUpdate(true, 0, 301, null, null)),
        400,
        "INVALID_PACKAGE_SOURCE");

    PackageSourceEntity unknown = source("PRIVATE");
    given(sourceRepository.findById(SOURCE_ID.toString())).willReturn(Optional.of(unknown));
    assertApiError(
        () ->
            service.updatePackageSource(
                SOURCE_ID, new AdminService.PackageSourceUpdate(true, 10, 30, null, null)),
        404,
        "NOT_FOUND");
  }

  @Test
  void systemConfigUsesTypedJsonAllowlistRangesUpdatedByAndExpectedVersions() throws Exception {
    SystemConfigEntity retention =
        new SystemConfigEntity(
            "artifactRetentionDays",
            objectMapper.readTree("30"),
            "Artifact retention in days",
            null,
            LocalDateTime.ofInstant(NOW.minusSeconds(60), ZoneOffset.UTC));
    given(configRepository.findAllById(any())).willReturn(List.of(retention));

    List<AdminService.SystemConfigView> updated =
        service.updateSystemConfig(
            ADMIN_ID,
            Map.of(
                "artifactRetentionDays",
                new AdminService.ConfigUpdate(objectMapper.readTree("60"), 0L)));

    assertThat(updated)
        .singleElement()
        .satisfies(view -> assertThat(view.value().asInt()).isEqualTo(60));
    assertThat(retention.getUpdatedBy()).isEqualTo(ADMIN_ID.toString());
    assertThat(retention.getUpdatedAt()).isEqualTo(LocalDateTime.ofInstant(NOW, ZoneOffset.UTC));

    assertApiError(
        () ->
            service.updateSystemConfig(
                ADMIN_ID,
                Map.of(
                    "arbitraryUrl",
                    new AdminService.ConfigUpdate(
                        objectMapper.valueToTree("https://evil.example"), 0L))),
        400,
        "UNKNOWN_CONFIG_KEY");
    assertApiError(
        () ->
            service.updateSystemConfig(
                ADMIN_ID,
                Map.of(
                    "artifactRetentionDays",
                    new AdminService.ConfigUpdate(objectMapper.readTree("\"30\""), 0L))),
        400,
        "INVALID_CONFIG_VALUE");
    assertApiError(
        () ->
            service.updateSystemConfig(
                ADMIN_ID,
                Map.of(
                    "artifactRetentionDays",
                    new AdminService.ConfigUpdate(objectMapper.readTree("0"), 0L))),
        400,
        "INVALID_CONFIG_VALUE");
    assertApiError(
        () ->
            service.updateSystemConfig(
                ADMIN_ID,
                Map.of(
                    "artifactRetentionDays",
                    new AdminService.ConfigUpdate(objectMapper.readTree("60"), 9L))),
        409,
        "CONFIG_VERSION_MISMATCH");
    assertApiError(
        () ->
            service.updateSystemConfig(
                ADMIN_ID,
                Map.of(
                    "artifactRetentionDays",
                    new AdminService.ConfigUpdate(objectMapper.readTree("60"), null))),
        400,
        "INVALID_CONFIG_VALUE");
  }

  private UserAccount user(UUID id, String username, String role, String status) {
    return new UserAccount(
        id.toString(), username, "$argon2id$encoded", role, status, LocalDateTime.now());
  }

  private PackageSourceEntity source(String code) {
    return new PackageSourceEntity(
        SOURCE_ID.toString(),
        code,
        "Tsinghua PyPI",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        10,
        true,
        30,
        0,
        LocalDateTime.ofInstant(NOW.minusSeconds(60), ZoneOffset.UTC));
  }

  private void assertApiError(Runnable operation, int status, String code) {
    assertThatThrownBy(operation::run)
        .isInstanceOf(ApiException.class)
        .satisfies(
            failure -> {
              ApiException apiFailure = (ApiException) failure;
              assertThat(apiFailure.status().value()).isEqualTo(status);
              assertThat(apiFailure.code()).isEqualTo(code);
            });
  }
}
