package com.wheelforge.api;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

@SpringBootTest(
    properties = {
      "spring.autoconfigure.exclude="
          + "org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration,"
          + "org.springframework.boot.hibernate.autoconfigure.HibernateJpaAutoConfiguration",
      "wheelforge.auth.token-secret=0123456789abcdef0123456789abcdef"
    })
class WheelForgeApplicationTest {
  @Autowired private ApplicationContext context;
  @MockitoBean private com.wheelforge.api.security.UserAccountRepository userAccountRepository;

  @Test
  void contextLoads() {
    assertThat(context.getBean(WheelForgeApplication.class)).isNotNull();
    assertThat(context.getBeansOfType(UserDetailsService.class)).isEmpty();
  }
}
