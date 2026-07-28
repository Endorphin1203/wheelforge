package com.wheelforge.api.artifact;

import jakarta.persistence.LockModeType;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface ArtifactRepository extends JpaRepository<ArtifactEntity, String> {
  @Query(
      """
      select artifact from ArtifactEntity artifact, BuildTaskEntity task
       where artifact.buildTaskId = task.id
         and task.userId = :userId
         and (:cursorCreatedAt is null
              or artifact.createdAt < :cursorCreatedAt
              or (artifact.createdAt = :cursorCreatedAt and artifact.id < :cursorId))
       order by artifact.createdAt desc, artifact.id desc
      """)
  List<ArtifactEntity> findPageByOwner(
      @Param("userId") String userId,
      @Param("cursorCreatedAt") LocalDateTime cursorCreatedAt,
      @Param("cursorId") String cursorId,
      Pageable pageable);

  @Query(
      """
      select artifact from ArtifactEntity artifact, BuildTaskEntity task
       where artifact.id = :id
         and artifact.buildTaskId = task.id
         and task.userId = :userId
      """)
  Optional<ArtifactEntity> findByIdAndOwner(@Param("id") String id, @Param("userId") String userId);

  @Lock(LockModeType.PESSIMISTIC_WRITE)
  @Query(
      """
      select artifact from ArtifactEntity artifact, BuildTaskEntity task
       where artifact.id = :id
         and artifact.buildTaskId = task.id
         and task.userId = :userId
      """)
  Optional<ArtifactEntity> findByIdAndOwnerForUpdate(
      @Param("id") String id, @Param("userId") String userId);

  @Query(
      value =
          """
          select * from artifacts
           where expires_at <= :now
             and cleaned_at is null
             and not exists (
               select 1 from download_records download
                where download.artifact_id = artifacts.id
                  and download.completed = false
                  and download.downloaded_at > :activeDownloadCutoff
             )
           order by expires_at, id
           limit :batchSize
           for update skip locked
          """,
      nativeQuery = true)
  List<ArtifactEntity> claimExpired(
      @Param("now") LocalDateTime now,
      @Param("activeDownloadCutoff") LocalDateTime activeDownloadCutoff,
      @Param("batchSize") int batchSize);
}
