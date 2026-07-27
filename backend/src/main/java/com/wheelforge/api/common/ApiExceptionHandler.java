package com.wheelforge.api.common;

import com.wheelforge.api.security.AuthService.AuthenticationFailedException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.dao.PessimisticLockingFailureException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.web.AuthenticationEntryPoint;
import org.springframework.security.web.access.AccessDeniedHandler;
import org.springframework.stereotype.Component;
import org.springframework.validation.FieldError;
import org.springframework.web.HttpMediaTypeNotAcceptableException;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.multipart.MaxUploadSizeExceededException;
import org.springframework.web.multipart.support.MissingServletRequestPartException;
import org.springframework.web.servlet.NoHandlerFoundException;
import org.springframework.web.servlet.resource.NoResourceFoundException;
import tools.jackson.databind.ObjectMapper;

@Component
@RestControllerAdvice
public class ApiExceptionHandler implements AuthenticationEntryPoint, AccessDeniedHandler {
  private static final Logger logger = LoggerFactory.getLogger(ApiExceptionHandler.class);

  private final ObjectMapper objectMapper;

  public ApiExceptionHandler(ObjectMapper objectMapper) {
    this.objectMapper = objectMapper;
  }

  @ExceptionHandler(ApiException.class)
  public void handleApiException(
      ApiException exception, HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    write(response, exception.status(), exception.code(), exception.getMessage(), Map.of());
  }

  @ExceptionHandler(MaxUploadSizeExceededException.class)
  public void handleUploadTooLarge(
      MaxUploadSizeExceededException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.PAYLOAD_TOO_LARGE,
        "FILE_TOO_LARGE",
        "Requirements file exceeds 512 KiB",
        Map.of());
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

  @ExceptionHandler({NoHandlerFoundException.class, NoResourceFoundException.class})
  public void handleNotFound(
      Exception exception, HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    write(
        response, HttpStatus.NOT_FOUND, "NOT_FOUND", "Requested resource was not found", Map.of());
  }

  @ExceptionHandler(HttpRequestMethodNotSupportedException.class)
  public void handleMethodNotAllowed(
      HttpRequestMethodNotSupportedException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.METHOD_NOT_ALLOWED,
        "METHOD_NOT_ALLOWED",
        "Request method is not supported",
        Map.of());
  }

  @ExceptionHandler(HttpMediaTypeNotSupportedException.class)
  public void handleUnsupportedMediaType(
      HttpMediaTypeNotSupportedException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.UNSUPPORTED_MEDIA_TYPE,
        "UNSUPPORTED_MEDIA_TYPE",
        "Request content type is not supported",
        Map.of());
  }

  @ExceptionHandler({
    MissingServletRequestParameterException.class,
    MissingServletRequestPartException.class
  })
  public void handleMissingParameter(
      Exception exception, HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.BAD_REQUEST,
        "VALIDATION_FAILED",
        "Request validation failed",
        Map.of());
  }

  @ExceptionHandler(MethodArgumentTypeMismatchException.class)
  public void handleTypeMismatch(
      MethodArgumentTypeMismatchException exception,
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

  @ExceptionHandler(HttpMediaTypeNotAcceptableException.class)
  public void handleNotAcceptable(
      HttpMediaTypeNotAcceptableException exception,
      HttpServletRequest request,
      HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.NOT_ACCEPTABLE,
        "NOT_ACCEPTABLE",
        "Requested response content type is not acceptable",
        Map.of());
  }

  @ExceptionHandler({
    OptimisticLockingFailureException.class,
    PessimisticLockingFailureException.class
  })
  public void handleConcurrentModification(
      RuntimeException exception, HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    write(
        response,
        HttpStatus.CONFLICT,
        "CONCURRENT_MODIFICATION",
        "The resource was modified concurrently",
        Map.of());
  }

  @ExceptionHandler(Exception.class)
  public void handleUnexpectedFailure(
      Exception exception, HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    String traceId = UUID.randomUUID().toString();
    logger.error("Unexpected API failure traceId={}", traceId, exception);
    write(
        response,
        HttpStatus.INTERNAL_SERVER_ERROR,
        "INTERNAL_ERROR",
        "An unexpected error occurred",
        Map.of(),
        traceId);
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
  @ExceptionHandler(AccessDeniedException.class)
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

  private void write(
      HttpServletResponse response,
      HttpStatus status,
      String code,
      String message,
      Map<String, String> fieldErrors,
      String traceId)
      throws IOException {
    if (response.isCommitted()) {
      return;
    }
    response.setStatus(status.value());
    response.setContentType(MediaType.APPLICATION_JSON_VALUE);
    objectMapper.writeValue(
        response.getOutputStream(), new ApiError(code, message, fieldErrors, traceId));
  }

  public record ApiError(
      String code, String message, Map<String, String> fieldErrors, String traceId) {}
}
