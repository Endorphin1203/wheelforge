package com.wheelforge.api.common.jobs;

import java.time.LocalDateTime;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface BuildJobRepository extends JpaRepository<BuildJobEntity, String> {
  Optional<BuildJobEntity> findBySubjectIdAndJobType(String subjectId, String jobType);

  @Modifying(flushAutomatically = true)
  @Query(
      """
      update BuildJobEntity job
         set job.status = 'CANCELLED',
             job.finishedAt = :finishedAt,
             job.versionNo = job.versionNo + 1
       where job.subjectId = :subjectId
         and job.jobType = 'BUILD'
         and job.status = 'READY'
      """)
  int cancelReadyBuildJob(
      @Param("subjectId") String subjectId, @Param("finishedAt") LocalDateTime finishedAt);
}
