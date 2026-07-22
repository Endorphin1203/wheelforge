package com.wheelforge.api;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;

@SpringBootTest(
    properties = {
      "spring.autoconfigure.exclude="
          + "org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration,"
          + "org.springframework.boot.hibernate.autoconfigure.HibernateJpaAutoConfiguration"
    })
class WheelForgeApplicationTest {
  @Autowired private ApplicationContext context;

  @Test
  void contextLoads() {
    assertThat(context.getBean(WheelForgeApplication.class)).isNotNull();
  }
}
