package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.Mockito;
import org.springframework.boot.DefaultApplicationArguments;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;

class AdminBootstrapTest {
  @Test
  void createsAnArgon2idAdministratorWhenTheUsersTableIsEmpty() throws Exception {
    UserAccountRepository repository = Mockito.mock(UserAccountRepository.class);
    PasswordEncoder passwordEncoder = Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();
    given(repository.count()).willReturn(0L);
    AdminBootstrap bootstrap =
        bootstrap(
            repository,
            passwordEncoder,
            environment("bootstrap-admin", "correct horse battery staple"));

    bootstrap.run(new DefaultApplicationArguments());

    ArgumentCaptor<UserAccount> account = ArgumentCaptor.forClass(UserAccount.class);
    verify(repository).save(account.capture());
    assertThat(account.getValue().getId()).matches("[0-9a-f-]{36}");
    assertThat(account.getValue().getUsername()).isEqualTo("bootstrap-admin");
    assertThat(account.getValue().getRole()).isEqualTo("ADMIN");
    assertThat(account.getValue().getStatus()).isEqualTo("ACTIVE");
    assertThat(account.getValue().getPasswordHash()).startsWith("$argon2id$");
    assertThat(
            passwordEncoder.matches(
                "correct horse battery staple", account.getValue().getPasswordHash()))
        .isTrue();
  }

  @Test
  void doesNothingWhenAUserAlreadyExists() throws Exception {
    UserAccountRepository repository = Mockito.mock(UserAccountRepository.class);
    PasswordEncoder passwordEncoder = Mockito.mock(PasswordEncoder.class);
    given(repository.count()).willReturn(1L);
    AdminBootstrap bootstrap =
        bootstrap(repository, passwordEncoder, environment("bootstrap-admin", "password"));

    bootstrap.run(new DefaultApplicationArguments());

    verify(repository, never()).save(org.mockito.ArgumentMatchers.any());
    verify(passwordEncoder, never()).encode(org.mockito.ArgumentMatchers.any());
  }

  @Test
  void refusesExactlyOneBootstrapVariable() {
    UserAccountRepository repository = Mockito.mock(UserAccountRepository.class);
    AdminBootstrap bootstrap =
        bootstrap(
            repository,
            Mockito.mock(PasswordEncoder.class),
            new MockEnvironment().withProperty("WF_BOOTSTRAP_ADMIN_USERNAME", "bootstrap-admin"));

    assertThatThrownBy(() -> bootstrap.run(new DefaultApplicationArguments()))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("must be configured together");
    verify(repository, never()).count();
  }

  @Test
  void refusesBlankBootstrapCredentials() {
    UserAccountRepository repository = Mockito.mock(UserAccountRepository.class);
    PasswordEncoder passwordEncoder = Mockito.mock(PasswordEncoder.class);
    given(repository.count()).willReturn(0L);
    AdminBootstrap bootstrap = bootstrap(repository, passwordEncoder, environment(" ", " "));

    assertThatThrownBy(() -> bootstrap.run(new DefaultApplicationArguments()))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("must not be blank");
    verify(repository, never()).save(org.mockito.ArgumentMatchers.any());
    verify(passwordEncoder, never()).encode(org.mockito.ArgumentMatchers.any());
  }

  private AdminBootstrap bootstrap(
      UserAccountRepository repository,
      PasswordEncoder passwordEncoder,
      MockEnvironment environment) {
    AdminBootstrap bootstrap = new AdminBootstrap(repository, passwordEncoder);
    bootstrap.setEnvironment(environment);
    return bootstrap;
  }

  private MockEnvironment environment(String username, String password) {
    return new MockEnvironment()
        .withProperty("WF_BOOTSTRAP_ADMIN_USERNAME", username)
        .withProperty("WF_BOOTSTRAP_ADMIN_PASSWORD", password);
  }
}
