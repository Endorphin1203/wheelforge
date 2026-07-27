package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThat;

import com.wheelforge.api.WheelForgeApplication;
import com.wheelforge.api.common.jobs.BuildJobRepository;
import com.wheelforge.api.common.storage.LocalFileStorage;
import com.wheelforge.api.requirements.RequirementFileRepository;
import com.wheelforge.api.requirements.RequirementItemRepository;
import com.wheelforge.api.target.TargetProfileRepository;
import java.lang.reflect.Proxy;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.context.runner.WebApplicationContextRunner;
import org.springframework.context.annotation.Bean;

class TokenSecretContextTest {
  private final WebApplicationContextRunner contextRunner =
      new WebApplicationContextRunner()
          .withUserConfiguration(WheelForgeApplication.class, RepositoryConfiguration.class)
          .withPropertyValues(
              "spring.autoconfigure.exclude="
                  + "org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration,"
                  + "org.springframework.boot.hibernate.autoconfigure.HibernateJpaAutoConfiguration");

  @Test
  void rejectsAContextWithoutTheTokenSecret() {
    contextRunner.run(
        context -> {
          assertThat(context).hasFailed();
          assertThat(context.getStartupFailure())
              .hasRootCauseInstanceOf(IllegalArgumentException.class);
        });
  }

  @Test
  void rejectsAContextWithAShortTokenSecret() {
    contextRunner
        .withPropertyValues("wheelforge.auth.token-secret=short")
        .run(
            context -> {
              assertThat(context).hasFailed();
              assertThat(context.getStartupFailure())
                  .hasRootCauseInstanceOf(IllegalStateException.class);
            });
  }

  @TestConfiguration(proxyBeanMethods = false)
  static class RepositoryConfiguration {
    @Bean
    UserAccountRepository userAccountRepository() {
      return repositoryProxy(UserAccountRepository.class);
    }

    @Bean
    BuildJobRepository buildJobRepository() {
      return repositoryProxy(BuildJobRepository.class);
    }

    @Bean
    RequirementFileRepository requirementFileRepository() {
      return repositoryProxy(RequirementFileRepository.class);
    }

    @Bean
    RequirementItemRepository requirementItemRepository() {
      return repositoryProxy(RequirementItemRepository.class);
    }

    @Bean
    TargetProfileRepository targetProfileRepository() {
      return repositoryProxy(TargetProfileRepository.class);
    }

    @Bean
    LocalFileStorage localFileStorage() {
      return new LocalFileStorage(
          Path.of(System.getProperty("java.io.tmpdir"), "wheelforge-token-secret-context-test"));
    }

    private static <T> T repositoryProxy(Class<T> repositoryType) {
      return repositoryType.cast(
          Proxy.newProxyInstance(
              repositoryType.getClassLoader(),
              new Class<?>[] {repositoryType},
              (proxy, method, args) -> {
                if (method.getName().equals("hashCode")) {
                  return System.identityHashCode(proxy);
                }
                if (method.getName().equals("equals")) {
                  return proxy == args[0];
                }
                if (method.getName().equals("toString")) {
                  return "test-" + repositoryType.getSimpleName();
                }
                throw new UnsupportedOperationException(method.getName());
              }));
    }
  }
}
