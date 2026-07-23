package com.wheelforge.api.security;

import com.wheelforge.api.common.ApiExceptionHandler;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.List;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.filter.OncePerRequestFilter;

@Configuration
@EnableWebSecurity
public class SecurityConfig {
  @Bean
  PasswordEncoder passwordEncoder() {
    return Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();
  }

  @Bean
  SecurityFilterChain securityFilterChain(
      HttpSecurity http, TokenService tokenService, ApiExceptionHandler apiExceptionHandler)
      throws Exception {
    return http.csrf(AbstractHttpConfigurer::disable)
        .sessionManagement(
            session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
        .exceptionHandling(
            exceptions ->
                exceptions
                    .authenticationEntryPoint(apiExceptionHandler)
                    .accessDeniedHandler(apiExceptionHandler))
        .authorizeHttpRequests(
            authorization ->
                authorization
                    .requestMatchers(HttpMethod.POST, "/api/auth/login")
                    .permitAll()
                    .requestMatchers("/actuator/health/**")
                    .permitAll()
                    .anyRequest()
                    .authenticated())
        .addFilterBefore(
            new TokenAuthenticationFilter(tokenService), UsernamePasswordAuthenticationFilter.class)
        .build();
  }

  private static final class TokenAuthenticationFilter extends OncePerRequestFilter {
    private final TokenService tokenService;

    private TokenAuthenticationFilter(TokenService tokenService) {
      this.tokenService = tokenService;
    }

    @Override
    protected void doFilterInternal(
        HttpServletRequest request, HttpServletResponse response, FilterChain filterChain)
        throws ServletException, IOException {
      String authorization = request.getHeader(HttpHeaders.AUTHORIZATION);
      if (authorization != null && authorization.startsWith("Bearer ")) {
        tokenService
            .parse(authorization.substring("Bearer ".length()))
            .ifPresent(
                currentUser -> {
                  var authentication =
                      new UsernamePasswordAuthenticationToken(
                          currentUser,
                          null,
                          List.of(new SimpleGrantedAuthority("ROLE_" + currentUser.role())));
                  SecurityContextHolder.getContext().setAuthentication(authentication);
                });
      }
      filterChain.doFilter(request, response);
    }
  }
}
