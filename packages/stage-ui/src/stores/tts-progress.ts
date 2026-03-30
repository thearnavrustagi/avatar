import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export const useTtsProgressStore = defineStore('tts-progress', () => {
  const totalSegments = ref(0)
  const completedSegments = ref(0)
  const active = ref(false)

  const progress = computed(() => {
    if (totalSegments.value === 0)
      return 0
    return Math.round((completedSegments.value / totalSegments.value) * 100)
  })

  function reset() {
    totalSegments.value = 0
    completedSegments.value = 0
  }

  function onIntentStart() {
    reset()
    active.value = true
  }

  function onTtsRequest() {
    totalSegments.value++
  }

  function onTtsResult() {
    completedSegments.value++
  }

  function onIntentEnd() {
    active.value = false
  }

  function onIntentCancel() {
    active.value = false
    reset()
  }

  return {
    totalSegments,
    completedSegments,
    active,
    progress,
    onIntentStart,
    onTtsRequest,
    onTtsResult,
    onIntentEnd,
    onIntentCancel,
  }
})
