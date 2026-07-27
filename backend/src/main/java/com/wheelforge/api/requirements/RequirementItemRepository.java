package com.wheelforge.api.requirements;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface RequirementItemRepository extends JpaRepository<RequirementItemEntity, String> {
  List<RequirementItemEntity> findAllByRequirementFileIdOrderByLineNoAsc(String requirementFileId);
}
