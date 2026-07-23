package com.wheelforge.api.security;

import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.EnvironmentAware;
import org.springframework.core.env.Environment;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

@Component
public class AdminBootstrap implements ApplicationRunner, EnvironmentAware {
  private static final String USERNAME_PROPERTY = "WF_BOOTSTRAP_ADMIN_USERNAME";
  private static final String PASSWORD_PROPERTY = "WF_BOOTSTRAP_ADMIN_PASSWORD";

  private final UserAccountRepository userAccountRepository;
  private final PasswordEncoder passwordEncoder;
  private final Supplier<AdminBootstrapCoordinator> coordinatorSupplier;
  private Environment environment;

  @Autowired
  public AdminBootstrap(
      UserAccountRepository userAccountRepository,
      PasswordEncoder passwordEncoder,
      ObjectProvider<AdminBootstrapCoordinator> coordinatorProvider) {
    this(
        userAccountRepository,
        passwordEncoder,
        (Supplier<AdminBootstrapCoordinator>) coordinatorProvider::getIfAvailable);
  }

  private AdminBootstrap(
      UserAccountRepository userAccountRepository,
      PasswordEncoder passwordEncoder,
      Supplier<AdminBootstrapCoordinator> coordinatorSupplier) {
    this.userAccountRepository = userAccountRepository;
    this.passwordEncoder = passwordEncoder;
    this.coordinatorSupplier = coordinatorSupplier;
  }

  AdminBootstrap(
      UserAccountRepository userAccountRepository,
      PasswordEncoder passwordEncoder,
      AdminBootstrapCoordinator coordinator) {
    this(userAccountRepository, passwordEncoder, () -> coordinator);
  }

  @Override
  public void setEnvironment(Environment environment) {
    this.environment = environment;
  }

  @Override
  public void run(ApplicationArguments args) {
    boolean hasUsername = environment.containsProperty(USERNAME_PROPERTY);
    boolean hasPassword = environment.containsProperty(PASSWORD_PROPERTY);
    if (hasUsername != hasPassword) {
      throw new IllegalStateException(
          "WF_BOOTSTRAP_ADMIN_USERNAME and WF_BOOTSTRAP_ADMIN_PASSWORD must be configured together");
    }
    if (!hasUsername) {
      return;
    }

    String username = environment.getProperty(USERNAME_PROPERTY);
    String password = environment.getProperty(PASSWORD_PROPERTY);
    if (!StringUtils.hasText(username) || !StringUtils.hasText(password)) {
      throw new IllegalStateException("Bootstrap administrator credentials must not be blank");
    }
    AdminBootstrapCoordinator coordinator = coordinatorSupplier.get();
    if (coordinator == null) {
      throw new IllegalStateException(
          "Bootstrap administrator initialization requires MySQL locking");
    }
    coordinator.runWithInitializationLock(
        () -> {
          if (userAccountRepository.count() != 0) {
            return;
          }
          userAccountRepository.saveAndFlush(
              new UserAccount(
                  UUID.randomUUID().toString(),
                  username,
                  passwordEncoder.encode(password),
                  "ADMIN",
                  "ACTIVE",
                  LocalDateTime.now(ZoneOffset.UTC)));
        });
  }
}
