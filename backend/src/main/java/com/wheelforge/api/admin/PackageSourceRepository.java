package com.wheelforge.api.admin;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface PackageSourceRepository extends JpaRepository<PackageSourceEntity, String> {
  List<PackageSourceEntity> findAllByOrderByPriorityNoAscCodeAsc();
}
