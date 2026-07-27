package com.wheelforge.api.target;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.util.List;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

@Entity
@Table(name = "target_profiles")
public class TargetProfileEntity {
  @Id
  @Column(name = "id", nullable = false, length = 36)
  private String id;

  @Column(name = "code", nullable = false, unique = true, length = 100)
  private String code;

  @Column(name = "os", nullable = false, length = 20)
  private String os;

  @Column(name = "architecture", nullable = false, length = 20)
  private String architecture;

  @Column(name = "python_implementation", nullable = false, length = 20)
  private String pythonImplementation;

  @Column(name = "python_version", nullable = false, length = 10)
  private String pythonVersion;

  @Column(name = "python_full_version", nullable = false, length = 20)
  private String pythonFullVersion;

  @Column(name = "platform_tag", nullable = false, length = 100)
  private String platformTag;

  @JdbcTypeCode(SqlTypes.JSON)
  @Column(name = "abi_tags", nullable = false, columnDefinition = "json")
  private List<String> abiTags;

  @Column(name = "validation_type", nullable = false, length = 20)
  private String validationType;

  @Column(name = "validation_policy_version", nullable = false, length = 50)
  private String validationPolicyVersion;

  @Column(name = "enabled", nullable = false)
  private boolean enabled;

  @Version
  @Column(name = "version_no", nullable = false)
  private long versionNo;

  protected TargetProfileEntity() {}

  public TargetProfileEntity(
      String id,
      String code,
      String os,
      String architecture,
      String pythonImplementation,
      String pythonVersion,
      String pythonFullVersion,
      String platformTag,
      List<String> abiTags,
      String validationType,
      String validationPolicyVersion,
      boolean enabled) {
    this.id = id;
    this.code = code;
    this.os = os;
    this.architecture = architecture;
    this.pythonImplementation = pythonImplementation;
    this.pythonVersion = pythonVersion;
    this.pythonFullVersion = pythonFullVersion;
    this.platformTag = platformTag;
    this.abiTags = List.copyOf(abiTags);
    this.validationType = validationType;
    this.validationPolicyVersion = validationPolicyVersion;
    this.enabled = enabled;
  }

  public String getId() {
    return id;
  }

  public String getCode() {
    return code;
  }

  public String getOs() {
    return os;
  }

  public String getArchitecture() {
    return architecture;
  }

  public String getPythonImplementation() {
    return pythonImplementation;
  }

  public String getPythonVersion() {
    return pythonVersion;
  }

  public String getPythonFullVersion() {
    return pythonFullVersion;
  }

  public String getPlatformTag() {
    return platformTag;
  }

  public List<String> getAbiTags() {
    return List.copyOf(abiTags);
  }

  public String getValidationType() {
    return validationType;
  }

  public String getValidationPolicyVersion() {
    return validationPolicyVersion;
  }

  public boolean isEnabled() {
    return enabled;
  }
}
