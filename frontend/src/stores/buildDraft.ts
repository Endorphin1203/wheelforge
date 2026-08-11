import { ref } from 'vue'
import { defineStore } from 'pinia'

export const useBuildDraftStore = defineStore('buildDraft', () => {
  const requirementFileId = ref<string | null>(null)
  const targetProfileId = ref<string | null>(null)

  function setRequirementFile(id: string): void {
    requirementFileId.value = id
  }

  function setTargetProfile(id: string): void {
    targetProfileId.value = id
  }

  function clear(): void {
    requirementFileId.value = null
    targetProfileId.value = null
  }

  return { requirementFileId, targetProfileId, setRequirementFile, setTargetProfile, clear }
})
