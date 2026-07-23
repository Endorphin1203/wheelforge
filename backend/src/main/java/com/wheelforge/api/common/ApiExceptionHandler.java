package com.wheelforge.api.common;

import com.wheelforge.api.security.AuthService.AuthenticationFailedException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.web.AuthenticationEntryPoint;
import org.springframework.security.web.access.AccessDeniedHandler;
import org.springframework.stereotype.Component;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import tools.jackson.databind.ObjectMapper;

@Component
@RestControllerAdvice
public class ApiExceptionHandler implements AuthenticationEntryPoint, AccessDeniedHandler {
  private final ObjectMapper objectMapper;

  public ApiExceptionHandler(ObjectMapper objectMapper) {
    this.objectMapper = objectMapper;
  }

  @ExceptionHandler(AuthenticationFailedException.class)
  public void handleAuthenticationFailure(
      AuthenticationFailedException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.UNAUTHORIZED,
        "AUTHENTICATION_FAILED",
        "Invalid username or password",
        Map.of());
  }

  @ExceptionHandler(MethodArgumentNotValidException.class)
  public void handleValidationFailure(
      MethodArgumentNotValidException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    Map<String, String> fieldErrors = new LinkedHashMap<>();
    for (FieldError fieldError : exception.getBindingResult().getFieldErrors()) {
      fieldErrors.put(fieldError.getField(), fieldError.getDefaultMessage());
    }
    write(
        response,
        HttpStatus.BAD_REQUEST,
        "VALIDATION_FAILED",
        "Request validation failed",
        fieldErrors);
  }

  @ExceptionHandler(HttpMessageNotReadableException.class)
  public void handleUnreadableRequest(
      HttpMessageNotReadableException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.BAD_REQUEST,
        "VALIDATION_FAILED",
        "Request validation failed",
        Map.of());
  }

  @Override
  public void commence(
      HttpServletRequest request,
      HttpServletResponse response,
      AuthenticationException authenticationException)
      throws IOException {
    write(
        response,
        HttpStatus.UNAUTHORIZED,
        "UNAUTHENTICATED",
        "Authentication is required",
        Map.of());
  }

  @Override
  public void handle(
      HttpServletRequest request,
      HttpServletResponse response,
      AccessDeniedException accessDeniedException)
      throws IOException {
    write(
        response,
        HttpStatus.FORBIDDEN,
        "FORBIDDEN",
        "You do not have permission to perform this action",
        Map.of());
  }

  private void write(
      HttpServletResponse response,
      HttpStatus status,
      String code,
      String message,
      Map<String, String> fieldErrors)
      throws IOException {
    if (response.isCommitted()) {
      return;
    }
    response.setStatus(status.value());
    response.setContentType(MediaType.APPLICATION_JSON_VALUE);
    objectMapper.writeValue(
        response.getOutputStream(),
        new ApiError(code, message, fieldErrors, UUID.randomUUID().toString()));
  }

  public record ApiError(
      String code, String message, Map<String, String> fieldErrors, String traceId) {}
}
