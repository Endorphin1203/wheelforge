package com.wheelforge.api.build;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface BuildTaskRepository extends JpaRepository<BuildTaskEntity, String> {
  List<BuildTaskEntity> findAllByUserIdAndDeletedAtIsNullOrderByCreatedAtDesc(String userId);

  Optional<BuildTaskEntity> findByIdAndUserIdAndDeletedAtIsNull(String id, String userId);

  Optional<BuildTaskEntity> findByIdAndUserId(String id, String userId);
}
