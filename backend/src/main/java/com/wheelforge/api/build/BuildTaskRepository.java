package com.wheelforge.api.build;

import jakarta.persistence.LockModeType;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface BuildTaskRepository extends JpaRepository<BuildTaskEntity, String> {
  List<BuildTaskEntity> findAllByUserIdAndDeletedAtIsNullOrderByCreatedAtDesc(String userId);

  Optional<BuildTaskEntity> findByIdAndUserIdAndDeletedAtIsNull(String id, String userId);

  Optional<BuildTaskEntity> findByIdAndUserId(String id, String userId);

  @Lock(LockModeType.PESSIMISTIC_WRITE)
  @Query(
      """
      select task from BuildTaskEntity task
       where task.id = :id
         and task.userId = :userId
         and task.deletedAt is null
      """)
  Optional<BuildTaskEntity> findByIdAndUserIdAndDeletedAtIsNullForUpdate(
      @Param("id") String id, @Param("userId") String userId);

  @Lock(LockModeType.PESSIMISTIC_WRITE)
  @Query(
      """
      select task from BuildTaskEntity task
       where task.id = :id
         and task.userId = :userId
      """)
  Optional<BuildTaskEntity> findByIdAndUserIdForUpdate(
      @Param("id") String id, @Param("userId") String userId);
}
