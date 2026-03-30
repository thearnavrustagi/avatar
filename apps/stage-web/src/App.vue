<script setup lang="ts">
import { OnboardingDialog, OnboardingStepAnalyticsNotice, ToasterRoot } from '@proj-airi/stage-ui/components'
import { isPosthogAvailableInBuild, useSharedAnalyticsStore } from '@proj-airi/stage-ui/stores/analytics'
import { useCharacterOrchestratorStore } from '@proj-airi/stage-ui/stores/character'
import { useChatSessionStore } from '@proj-airi/stage-ui/stores/chat/session-store'
import { useDisplayModelsStore } from '@proj-airi/stage-ui/stores/display-models'
import { useModsServerChannelStore } from '@proj-airi/stage-ui/stores/mods/api/channel-server'
import { useContextBridgeStore } from '@proj-airi/stage-ui/stores/mods/api/context-bridge'
import { useAiriCardStore } from '@proj-airi/stage-ui/stores/modules/airi-card'
import { useConsciousnessStore } from '@proj-airi/stage-ui/stores/modules/consciousness'
import { useHearingStore } from '@proj-airi/stage-ui/stores/modules/hearing'
import { useSpeechStore } from '@proj-airi/stage-ui/stores/modules/speech'
import { useOnboardingStore } from '@proj-airi/stage-ui/stores/onboarding'
import { useProvidersStore } from '@proj-airi/stage-ui/stores/providers'
import { useSettings, useSettingsAudioDevice } from '@proj-airi/stage-ui/stores/settings'
import { useTheme } from '@proj-airi/ui'
import { StageTransitionGroup } from '@proj-airi/ui-transitions'
import { storeToRefs } from 'pinia'
import { computed, onMounted, onUnmounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterView } from 'vue-router'
import { toast, Toaster } from 'vue-sonner'

import PerformanceOverlay from './components/Devtools/PerformanceOverlay.vue'

import { usePWAStore } from './stores/pwa'

usePWAStore()

const contextBridgeStore = useContextBridgeStore()
const i18n = useI18n()
const displayModelsStore = useDisplayModelsStore()
const settingsStore = useSettings()
const settings = storeToRefs(settingsStore)
const onboardingStore = useOnboardingStore()
const chatSessionStore = useChatSessionStore()
const serverChannelStore = useModsServerChannelStore()
const characterOrchestratorStore = useCharacterOrchestratorStore()
const settingsAudioDeviceStore = useSettingsAudioDevice()
const { showingSetup } = storeToRefs(onboardingStore)
const { isDark } = useTheme()
const cardStore = useAiriCardStore()
const analyticsStore = useSharedAnalyticsStore()
const consciousnessStore = useConsciousnessStore()
const hearingStore = useHearingStore()
const speechStore = useSpeechStore()
const providersStore = useProvidersStore()

const primaryColor = computed(() => {
  return isDark.value
    ? `color-mix(in srgb, oklch(95% var(--chromatic-chroma-900) calc(var(--chromatic-hue) + ${0})) 70%, oklch(50% 0 360))`
    : `color-mix(in srgb, oklch(95% var(--chromatic-chroma-900) calc(var(--chromatic-hue) + ${0})) 90%, oklch(90% 0 360))`
})

const secondaryColor = computed(() => {
  return isDark.value
    ? `color-mix(in srgb, oklch(95% var(--chromatic-chroma-900) calc(var(--chromatic-hue) + ${180})) 70%, oklch(50% 0 360))`
    : `color-mix(in srgb, oklch(95% var(--chromatic-chroma-900) calc(var(--chromatic-hue) + ${180})) 90%, oklch(90% 0 360))`
})

const tertiaryColor = computed(() => {
  return isDark.value
    ? `color-mix(in srgb, oklch(95% var(--chromatic-chroma-900) calc(var(--chromatic-hue) + ${60})) 70%, oklch(50% 0 360))`
    : `color-mix(in srgb, oklch(95% var(--chromatic-chroma-900) calc(var(--chromatic-hue) + ${60})) 90%, oklch(90% 0 360))`
})

const colors = computed(() => {
  return [primaryColor.value, secondaryColor.value, tertiaryColor.value, isDark.value ? '#121212' : '#FFFFFF']
})

const onboardingExtraSteps = computed(() => {
  return isPosthogAvailableInBuild()
    ? [{ id: 'analytics-notice', component: OnboardingStepAnalyticsNotice }]
    : []
})

watch(settings.language, () => {
  i18n.locale.value = settings.language.value
})

watch(settings.themeColorsHue, () => {
  document.documentElement.style.setProperty('--chromatic-hue', settings.themeColorsHue.value.toString())
}, { immediate: true })

watch(settings.themeColorsHueDynamic, () => {
  document.documentElement.classList.toggle('dynamic-hue', settings.themeColorsHueDynamic.value)
}, { immediate: true })

// Initialize first-time setup check when app mounts
onMounted(async () => {
  analyticsStore.initialize()

  // Auto-configure from .env BEFORE card init so the card picks up the right providers
  const geminiKey = import.meta.env.VITE_GEMINI_API_KEY
  if (geminiKey) {
    if (!consciousnessStore.activeProvider) {
      consciousnessStore.activeProvider = 'google-generative-ai'
      consciousnessStore.activeModel = import.meta.env.VITE_GEMINI_MODEL || 'gemini-2.0-flash'
    }
    // Force kokoro-server to use the remote TTS endpoint.
    // Skip validateProvider — the remote server doesn't expose /models,
    // so validation would fail and a watcher in speech.ts would reset
    // the provider back to speech-noop.
    providersStore.providers['kokoro-server'] = { baseUrl: 'https://ttsserver.internal.quizizz.com/_ttsserver/main/v1/' }
    providersStore.markProviderAdded('kokoro-server')
    providersStore.initializeProvider('kokoro-server')
    // Mark as validated manually so the speech watcher doesn't reset it
    if (providersStore.providerRuntimeState['kokoro-server']) {
      providersStore.providerRuntimeState['kokoro-server'].isConfigured = true
      providersStore.providerRuntimeState['kokoro-server'].validatedCredentialHash = 'force-configured'
    }
    speechStore.activeSpeechProvider = 'kokoro-server'
    speechStore.activeSpeechModel = 'kokoro'
    speechStore.activeSpeechVoiceId = 'am_adam'
    // eslint-disable-next-line no-console
    console.debug('[App] Configured kokoro-server TTS with remote URL')
    // Auto-configure Whisper transcription via Kokoro server (STT)
    if (!hearingStore.activeTranscriptionProvider || hearingStore.activeTranscriptionProvider === 'browser-web-speech-api') {
      providersStore.providers['kokoro-server-transcription'] = { baseUrl: 'http://localhost:8880/v1/' }
      providersStore.markProviderAdded('kokoro-server-transcription')
      providersStore.initializeProvider('kokoro-server-transcription')
      await providersStore.validateProvider('kokoro-server-transcription', { force: true })
      hearingStore.activeTranscriptionProvider = 'kokoro-server-transcription'
      hearingStore.activeTranscriptionModel = 'large-v3-turbo'
      hearingStore.autoSendEnabled = false
      // eslint-disable-next-line no-console
      console.debug('[App] Auto-configured kokoro-server-transcription for STT')
    }

    onboardingStore.markSetupCompleted()
  }

  cardStore.initialize()

  if (onboardingStore.needsOnboarding) {
    onboardingStore.showingSetup = true
  }

  await chatSessionStore.initialize()

  // Initialize display models and stage model early so the 3D model renders immediately.
  // These do not depend on the server channel or context bridge.
  // eslint-disable-next-line no-console
  console.debug('[App] Loading display models from IndexedDB...')
  await displayModelsStore.loadDisplayModelsFromIndexedDB()
  // eslint-disable-next-line no-console
  console.debug('[App] Display models loaded, count:', displayModelsStore.displayModels.length)
  // eslint-disable-next-line no-console
  console.debug('[App] Initializing stage model...')
  await settingsStore.initializeStageModel()
  // eslint-disable-next-line no-console
  console.debug('[App] Stage model initialized')
  await settingsAudioDeviceStore.initialize()

  // Server channel and context bridge are non-critical for rendering.
  // Initialize them after the stage model so the UI is not blocked if
  // the WebSocket server is unavailable.
  await serverChannelStore.initialize({ possibleEvents: ['ui:configure'] }).catch(err => console.error('Failed to initialize Mods Server Channel in App.vue:', err))
  contextBridgeStore.initialize().catch(err => console.error('Failed to initialize Context Bridge:', err))
  characterOrchestratorStore.initialize()
})

onUnmounted(() => {
  contextBridgeStore.dispose()
})

// Handle first-time setup events
function handleSetupConfigured() {
  onboardingStore.markSetupCompleted()
}

function handleSetupSkipped() {
  onboardingStore.markSetupSkipped()
}
</script>

<template>
  <StageTransitionGroup
    :primary-color="primaryColor"
    :secondary-color="secondaryColor"
    :tertiary-color="tertiaryColor"
    :colors="colors"
    :z-index="100"
    :disable-transitions="settings.disableTransitions.value"
    :use-page-specific-transitions="settings.usePageSpecificTransitions.value"
  >
    <RouterView v-slot="{ Component }">
      <component :is="Component" />
    </RouterView>
  </StageTransitionGroup>

  <ToasterRoot @close="id => toast.dismiss(id)">
    <Toaster />
  </ToasterRoot>

  <!-- First Time Setup Dialog -->
  <OnboardingDialog
    v-model="showingSetup"
    :extra-steps="onboardingExtraSteps"
    @configured="handleSetupConfigured"
    @skipped="handleSetupSkipped"
  />

  <PerformanceOverlay />
</template>

<style>
/* We need this to properly animate the CSS variable */
@property --chromatic-hue {
  syntax: '<number>';
  initial-value: 0;
  inherits: true;
}

@keyframes hue-anim {
  from {
    --chromatic-hue: 0;
  }
  to {
    --chromatic-hue: 360;
  }
}

.dynamic-hue {
  animation: hue-anim 10s linear infinite;
}
</style>
