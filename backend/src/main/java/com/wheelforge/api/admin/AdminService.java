package com.wheelforge.api.admin;

import com.wheelforge.api.common.ApiException;
import com.wheelforge.api.security.UserAccount;
import com.wheelforge.api.security.UserAccountRepository;
import java.security.SecureRandom;
import java.time.Clock;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

@Service
public class AdminService {
  private static final Set<String> BUILT_IN_SOURCES = Set.of("TSINGHUA", "ALIYUN", "PYPI");
  private static final Pattern USERNAME = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,99}");
  private static final Map<String, ConfigRule> CONFIG_RULES = configRules();

  private final UserAccountRepository userRepository;
  private final PackageSourceRepository sourceRepository;
  private final SystemConfigRepository configRepository;
  private final PasswordEncoder passwordEncoder;
  private final ObjectMapper objectMapper;
  private final SecureRandom secureRandom;
  private final Clock clock;

  @Autowired
  public AdminService(
      UserAccountRepository userRepository,
      PackageSourceRepository sourceRepository,
      SystemConfigRepository configRepository,
      PasswordEncoder passwordEncoder,
      ObjectMapper objectMapper) {
    this(
        userRepository,
        sourceRepository,
        configRepository,
        passwordEncoder,
        objectMapper,
        new SecureRandom(),
        Clock.systemUTC());
  }

  AdminService(
      UserAccountRepository userRepository,
      PackageSourceRepository sourceRepository,
      SystemConfigRepository configRepository,
      PasswordEncoder passwordEncoder,
      ObjectMapper objectMapper,
      SecureRandom secureRandom,
      Clock clock) {
    this.userRepository = userRepository;
    this.sourceRepository = sourceRepository;
    this.configRepository = configRepository;
    this.passwordEncoder = passwordEncoder;
    this.objectMapper = objectMapper;
    this.secureRandom = secureRandom;
    this.clock = clock;
  }

  @Transactional(readOnly = true)
  public List<UserView> users() {
    return userRepository.findAll().stream()
        .sorted(Comparator.comparing(UserAccount::getCreatedAt).reversed())
        .map(user -> userView(user, null))
        .toList();
  }

  @Transactional
  public UserView createUser(UUID administratorId, CreateUserRequest request) {
    String username = request.username() == null ? "" : request.username().trim();
    if (!USERNAME.matcher(username).matches()) {
      throw ApiException.badRequest(
          "INVALID_USERNAME", "Username must use 1 to 100 safe characters");
    }
    requireRole(request.role());
    if (userRepository.findByUsername(username).isPresent()) {
      throw ApiException.conflict("USERNAME_EXISTS", "Username already exists");
    }

    boolean generated = request.initialPassword() == null;
    String initialPassword = generated ? generatePassword() : request.initialPassword();
    requireStrongPassword(initialPassword);
    UserAccount user =
        new UserAccount(
            UUID.randomUUID().toString(),
            username,
            passwordEncoder.encode(initialPassword),
            request.role(),
            "ACTIVE",
            now());
    try {
      userRepository.saveAndFlush(user);
    } catch (DataIntegrityViolationException exception) {
      throw ApiException.conflict("USERNAME_EXISTS", "Username already exists");
    }
    return userView(user, generated ? initialPassword : null);
  }

  @Transactional
  public UserView updateUserStatus(
      UUID administratorId, UUID userId, UpdateUserStatusRequest request) {
    if (!"ACTIVE".equals(request.status()) && !"DISABLED".equals(request.status())) {
      throw ApiException.badRequest(
          "INVALID_USER_STATUS", "User status must be ACTIVE or DISABLED");
    }
    if (administratorId.equals(userId) && "DISABLED".equals(request.status())) {
      throw ApiException.conflict(
          "CANNOT_DISABLE_SELF", "An administrator cannot disable their own account");
    }
    UserAccount user =
        userRepository
            .findById(userId.toString())
            .orElseThrow(() -> ApiException.notFound("User was not found"));
    user.updateStatus(request.status());
    return userView(user, null);
  }

  @Transactional(readOnly = true)
  public List<PackageSourceView> packageSources() {
    return sourceRepository.findAllByOrderByPriorityNoAscCodeAsc().stream()
        .filter(source -> BUILT_IN_SOURCES.contains(source.getCode()))
        .map(this::sourceView)
        .toList();
  }

  @Transactional
  public PackageSourceView updatePackageSource(UUID sourceId, PackageSourceUpdate request) {
    if (request.code() != null || request.baseUrl() != null) {
      throw ApiException.badRequest(
          "IMMUTABLE_SOURCE_FIELD", "Package source code and URL cannot be changed");
    }
    if (request.enabled() == null
        || request.priorityNo() == null
        || request.timeoutSeconds() == null
        || request.priorityNo() < 1
        || request.priorityNo() > 1000
        || request.timeoutSeconds() < 1
        || request.timeoutSeconds() > 300) {
      throw ApiException.badRequest(
          "INVALID_PACKAGE_SOURCE", "Package source values are outside the allowed range");
    }
    PackageSourceEntity source =
        sourceRepository
            .findById(sourceId.toString())
            .filter(value -> BUILT_IN_SOURCES.contains(value.getCode()))
            .orElseThrow(() -> ApiException.notFound("Package source was not found"));
    source.update(request.enabled(), request.priorityNo(), request.timeoutSeconds(), now());
    sourceRepository.flush();
    return sourceView(source);
  }

  @Transactional(readOnly = true)
  public List<SystemConfigView> systemConfig() {
    return configRepository.findAllByOrderByConfigKeyAsc().stream()
        .filter(config -> CONFIG_RULES.containsKey(config.getConfigKey()))
        .map(this::configView)
        .toList();
  }

  @Transactional
  public List<SystemConfigView> updateSystemConfig(
      UUID administratorId, Map<String, ConfigUpdate> updates) {
    if (updates == null) {
      throw ApiException.badRequest("INVALID_CONFIG_VALUE", "Configuration updates are required");
    }
    for (var entry : updates.entrySet()) {
      ConfigRule rule = CONFIG_RULES.get(entry.getKey());
      if (rule == null) {
        throw ApiException.badRequest(
            "UNKNOWN_CONFIG_KEY", "Unknown system configuration key: " + entry.getKey());
      }
      if (entry.getValue() == null) {
        throw ApiException.badRequest("INVALID_CONFIG_VALUE", "Configuration update is required");
      }
      if (entry.getValue().version() == null || entry.getValue().version() < 0) {
        throw ApiException.badRequest(
            "INVALID_CONFIG_VALUE", "Configuration version must be non-negative");
      }
      rule.validate(entry.getValue().value());
    }

    List<SystemConfigEntity> configs = configRepository.findAllById(updates.keySet());
    Map<String, SystemConfigEntity> byKey =
        configs.stream()
            .collect(Collectors.toMap(SystemConfigEntity::getConfigKey, config -> config));
    if (byKey.size() != updates.size()) {
      throw ApiException.conflict(
          "CONFIG_NOT_INITIALIZED", "System configuration defaults are not initialized");
    }

    LocalDateTime now = now();
    for (var entry : updates.entrySet()) {
      SystemConfigEntity config = byKey.get(entry.getKey());
      if (config.getVersionNo() != entry.getValue().version()) {
        throw ApiException.conflict(
            "CONFIG_VERSION_MISMATCH", "System configuration version does not match");
      }
      JsonNode normalized = normalize(CONFIG_RULES.get(entry.getKey()), entry.getValue().value());
      config.update(normalized, administratorId.toString(), now);
    }
    configRepository.flush();
    return configs.stream()
        .sorted(Comparator.comparing(SystemConfigEntity::getConfigKey))
        .map(this::configView)
        .toList();
  }

  private void requireRole(String role) {
    if (!"USER".equals(role) && !"ADMIN".equals(role)) {
      throw ApiException.badRequest("INVALID_USER_ROLE", "User role must be USER or ADMIN");
    }
  }

  private void requireStrongPassword(String password) {
    if (password == null
        || password.length() < 12
        || password.length() > 128
        || password.chars().noneMatch(Character::isUpperCase)
        || password.chars().noneMatch(Character::isLowerCase)
        || password.chars().noneMatch(Character::isDigit)
        || password.chars().allMatch(Character::isLetterOrDigit)) {
      throw ApiException.badRequest(
          "INVALID_INITIAL_PASSWORD",
          "Initial password must be 12 to 128 characters with mixed character classes");
    }
  }

  private String generatePassword() {
    byte[] random = new byte[24];
    secureRandom.nextBytes(random);
    return "A1!" + Base64.getUrlEncoder().withoutPadding().encodeToString(random);
  }

  private LocalDateTime now() {
    return LocalDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
  }

  private JsonNode normalize(ConfigRule rule, JsonNode value) {
    return rule.type() == ValueType.BOOLEAN
        ? objectMapper.valueToTree(value.booleanValue())
        : objectMapper.valueToTree(value.longValue());
  }

  private UserView userView(UserAccount user, String initialPassword) {
    return new UserView(
        UUID.fromString(user.getId()),
        user.getUsername(),
        user.getRole(),
        user.getStatus(),
        user.getCreatedAt(),
        initialPassword);
  }

  private PackageSourceView sourceView(PackageSourceEntity source) {
    return new PackageSourceView(
        UUID.fromString(source.getId()),
        source.getCode(),
        source.getDisplayName(),
        source.getBaseUrl(),
        source.getPriorityNo(),
        source.isEnabled(),
        source.getTimeoutSeconds(),
        source.getFailureCount(),
        source.getUpdatedAt(),
        source.getVersionNo());
  }

  private SystemConfigView configView(SystemConfigEntity config) {
    return new SystemConfigView(
        config.getConfigKey(),
        config.getConfigValue(),
        config.getDescription(),
        config.getUpdatedBy() == null ? null : UUID.fromString(config.getUpdatedBy()),
        config.getUpdatedAt(),
        config.getVersionNo());
  }

  private static Map<String, ConfigRule> configRules() {
    Map<String, ConfigRule> rules = new LinkedHashMap<>();
    rules.put("maxUploadSizeBytes", ConfigRule.integer(1, 10L * 1024 * 1024));
    rules.put("maxRequirementLines", ConfigRule.integer(1, 10000));
    rules.put("maxPackageCount", ConfigRule.integer(1, 5000));
    rules.put("maxPackageSizeBytes", ConfigRule.integer(1, 2L * 1024 * 1024 * 1024));
    rules.put("maxArtifactSizeBytes", ConfigRule.integer(1, 10L * 1024 * 1024 * 1024));
    rules.put("minFreeDiskBytes", ConfigRule.integer(0, 1024L * 1024 * 1024 * 1024));
    rules.put("taskTimeoutSeconds", ConfigRule.integer(60, 86400));
    rules.put("maxConcurrentBuilds", ConfigRule.integer(1, 64));
    rules.put("maxRetryAttempts", ConfigRule.integer(0, 20));
    rules.put("maxCandidatesPerRequirement", ConfigRule.integer(1, 100));
    rules.put("maxResolutionAttempts", ConfigRule.integer(1, 1000));
    rules.put("maxArchiveEntries", ConfigRule.integer(1, 100000));
    rules.put("maxArchiveExpansionRatio", ConfigRule.integer(1, 1000));
    rules.put("artifactRetentionDays", ConfigRule.integer(1, 3650));
    rules.put("retentionEnabled", ConfigRule.bool());
    return Map.copyOf(rules);
  }

  public record CreateUserRequest(String username, String role, String initialPassword) {}

  public record UpdateUserStatusRequest(String status) {}

  public record UserView(
      UUID id,
      String username,
      String role,
      String status,
      LocalDateTime createdAt,
      String initialPassword) {}

  public record PackageSourceUpdate(
      Boolean enabled, Integer priorityNo, Integer timeoutSeconds, String code, String baseUrl) {}

  public record PackageSourceView(
      UUID id,
      String code,
      String displayName,
      String baseUrl,
      int priorityNo,
      boolean enabled,
      int timeoutSeconds,
      long failureCount,
      LocalDateTime updatedAt,
      long version) {}

  public record ConfigUpdate(JsonNode value, Long version) {}

  public record SystemConfigView(
      String key,
      JsonNode value,
      String description,
      UUID updatedBy,
      LocalDateTime updatedAt,
      long version) {}

  private record ConfigRule(ValueType type, long minimum, long maximum) {
    static ConfigRule integer(long minimum, long maximum) {
      return new ConfigRule(ValueType.INTEGER, minimum, maximum);
    }

    static ConfigRule bool() {
      return new ConfigRule(ValueType.BOOLEAN, 0, 0);
    }

    void validate(JsonNode value) {
      if (value == null
          || value.isNull()
          || (type == ValueType.INTEGER
              && (!value.isIntegralNumber()
                  || !value.canConvertToLong()
                  || value.longValue() < minimum
                  || value.longValue() > maximum))
          || (type == ValueType.BOOLEAN && !value.isBoolean())) {
        throw ApiException.badRequest(
            "INVALID_CONFIG_VALUE", "System configuration value has an invalid type or range");
      }
    }
  }

  private enum ValueType {
    INTEGER,
    BOOLEAN
  }
}
