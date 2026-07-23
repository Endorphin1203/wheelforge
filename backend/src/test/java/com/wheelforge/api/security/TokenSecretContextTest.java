package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThat;

import com.wheelforge.api.WheelForgeApplication;
import java.lang.reflect.Proxy;
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
      return (UserAccountRepository)
          Proxy.newProxyInstance(
              getClass().getClassLoader(),
              new Class<?>[] {UserAccountRepository.class},
              (proxy, method, args) -> {
                if (method.getName().equals("hashCode")) {
                  return System.identityHashCode(proxy);
                }
                if (method.getName().equals("equals")) {
                  return proxy == args[0];
                }
                if (method.getName().equals("toString")) {
                  return "test-user-account-repository";
                }
                throw new UnsupportedOperationException(method.getName());
              });
    }
  }
}
