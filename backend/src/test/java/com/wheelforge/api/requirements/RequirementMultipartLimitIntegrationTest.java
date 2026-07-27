package com.wheelforge.api.requirements;

import static java.nio.charset.StandardCharsets.UTF_8;
import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import com.wheelforge.api.common.ApiExceptionHandler;
import com.wheelforge.api.security.CurrentUser;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.AnonymousAuthenticationFilter;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.web.filter.OncePerRequestFilter;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest(
    classes = RequirementMultipartLimitIntegrationTest.TestApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = {
      "spring.autoconfigure.exclude="
          + "org.springframework.boot.jdbc.autoconfigure.DataSourceAutoConfiguration,"
          + "org.springframework.boot.hibernate.autoconfigure.HibernateJpaAutoConfiguration"
    })
class RequirementMultipartLimitIntegrationTest {
  private static final UUID USER_ID = UUID.fromString("cae6ea8d-0afe-41df-aeea-fc0e5aceabfb");
  private static final UUID FILE_ID = UUID.fromString("2b0e75d6-c238-4f39-9bf9-5e246eb7c4cd");
  private static final String BOUNDARY = "WheelForgeMultipartBoundary";

  @LocalServerPort private int port;
  @Autowired private ObjectMapper objectMapper;
  @MockitoBean private RequirementFileService requirementFileService;

  @Test
  void rejectsA524289ByteFileBeforeCallingTheService() throws Exception {
    HttpResponse<String> response = sendMultipart(new byte[RequirementFileService.MAX_BYTES + 1]);

    assertThat(response.statusCode()).isEqualTo(413);
    var body = objectMapper.readTree(response.body());
    assertThat(body.get("code").asString()).isEqualTo("FILE_TOO_LARGE");
    assertThat(body.get("fieldErrors").isObject()).isTrue();
    assertThat(body.get("traceId").asString()).isNotBlank();
    verify(requirementFileService, never()).upload(any(), any());
  }

  @Test
  void allowsA524288ByteFileThroughTheMultipartLayer() throws Exception {
    var view =
        new RequirementFileService.RequirementFileView(
            FILE_ID,
            "requirements.txt",
            RequirementFileService.MAX_BYTES,
            "61b547565054050327bdb8618040d14e5f5e40af571975ad5bf63c36f545c9d7",
            "PENDING",
            null,
            Instant.parse("2026-07-23T01:00:00Z"));
    org.mockito.BDDMockito.given(requirementFileService.upload(eq(USER_ID), any()))
        .willReturn(view);

    HttpResponse<String> response = sendMultipart(new byte[RequirementFileService.MAX_BYTES]);

    assertThat(response.statusCode()).isEqualTo(202);
    verify(requirementFileService).upload(eq(USER_ID), any());
  }

  private HttpResponse<String> sendMultipart(byte[] content) throws Exception {
    byte[] body = multipartBody(content);
    HttpRequest request =
        HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + port + "/api/requirement-files"))
            .header("Content-Type", "multipart/form-data; boundary=" + BOUNDARY)
            .POST(HttpRequest.BodyPublishers.ofByteArray(body))
            .build();
    return HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofString(UTF_8));
  }

  private byte[] multipartBody(byte[] content) throws IOException {
    var body = new ByteArrayOutputStream();
    body.write(("--" + BOUNDARY + "\r\n").getBytes(UTF_8));
    body.write(
        "Content-Disposition: form-data; name=\"file\"; filename=\"requirements.txt\"\r\n"
            .getBytes(UTF_8));
    body.write("Content-Type: text/plain\r\n\r\n".getBytes(UTF_8));
    body.write(content);
    body.write(("\r\n--" + BOUNDARY + "--\r\n").getBytes(UTF_8));
    return body.toByteArray();
  }

  @Configuration(proxyBeanMethods = false)
  @EnableAutoConfiguration
  @Import({RequirementFileController.class, ApiExceptionHandler.class, TestSecurityConfig.class})
  static class TestApplication {}

  @TestConfiguration
  static class TestSecurityConfig {
    @Bean
    SecurityFilterChain testSecurityFilterChain(HttpSecurity http) throws Exception {
      return http.csrf(AbstractHttpConfigurer::disable)
          .authorizeHttpRequests(authorization -> authorization.anyRequest().permitAll())
          .addFilterBefore(new CurrentUserFilter(), AnonymousAuthenticationFilter.class)
          .build();
    }
  }

  private static final class CurrentUserFilter extends OncePerRequestFilter {
    @Override
    protected void doFilterInternal(
        HttpServletRequest request, HttpServletResponse response, FilterChain filterChain)
        throws ServletException, IOException {
      var user = new CurrentUser(USER_ID, "USER");
      SecurityContextHolder.getContext()
          .setAuthentication(
              new UsernamePasswordAuthenticationToken(
                  user, null, List.of(new SimpleGrantedAuthority("ROLE_USER"))));
      filterChain.doFilter(request, response);
    }
  }
}
