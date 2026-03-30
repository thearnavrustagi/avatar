<script setup lang="ts">
/**
 * Stage Scene — the main rendering surface for the avatar model and speech pipeline.
 *
 * ## Speech / Lipsync Pipeline
 *
 * The pipeline flows as follows:
 *
 *   1. **Chat orchestrator** receives user input and streams tokens from the LLM
 *      (e.g. Gemini via `google-generative-ai` provider).
 *   2. Literal tokens are forwarded to a **SpeechPipeline** (`createSpeechPipeline`)
 *      which batches text and calls the active TTS provider (e.g. `kokoro-local`,
 *      `elevenlabs`, or `openai-compatible-audio-speech`).
 *   3. The TTS provider returns an `AudioBuffer` which is played through a
 *      **PlaybackManager** (`createPlaybackManager`).
 *   4. During playback, the audio is connected to a **lipsync analyzer**:
 *      - **Live2D**: `createLive2DLipSync` (wlipsync WASM) reads audio frames via
 *        an `AudioNode` and outputs a `mouthOpenSize` value per frame.
 *      - **VRM**: `useVRMLipSync` (in `@proj-airi/stage-ui-three`) connects the
 *        `currentAudioSource` AudioBufferSourceNode to a wLipSync node and maps
 *        viseme weights to VRM blend shapes each frame.
 *   5. Special tokens (emotions, delays) are routed through separate queues
 *      (`emotionsQueue`, `delaysQueue`) to trigger model expressions/motions.
 *
 * ## Prerequisites for speech to work
 *
 * - A **chat provider** must be configured with a valid API key
 *   (e.g. `VITE_GEMINI_API_KEY` in `.env`).
 * - A **speech provider** must be configured:
 *   - `kokoro-local`: Requires ONNX model download (~80MB for q8).
 *     The model is loaded via `KokoroWorkerManager.loadModel()`.
 *     When auto-configured via `.env`, `App.vue` triggers this automatically.
 *   - Cloud providers (ElevenLabs, OpenAI-compatible): Need API keys.
 * - The browser **AudioContext** must be resumed (requires user interaction).
 */
import type { DuckDBWasmDrizzleDatabase } from '@proj-airi/drizzle-duckdb-wasm'
import type { Live2DLipSync, Live2DLipSyncOptions } from '@proj-airi/model-driver-lipsync'
import type { Profile } from '@proj-airi/model-driver-lipsync/shared/wlipsync'
import type { TextSegment, TextToken } from '@proj-airi/pipelines-audio'

import type { EmotionPayload } from '../../constants/emotions'

import { drizzle } from '@proj-airi/drizzle-duckdb-wasm'
import { getImportUrlBundles } from '@proj-airi/drizzle-duckdb-wasm/bundles/import-url-browser'
import { createLive2DLipSync } from '@proj-airi/model-driver-lipsync'
import { wlipsyncProfile } from '@proj-airi/model-driver-lipsync/shared/wlipsync'
import { createPlaybackManager, createPushStream, createSpeechPipeline, createTtsSegmentStream } from '@proj-airi/pipelines-audio'
import { Live2DScene, useLive2d } from '@proj-airi/stage-ui-live2d'
import { ThreeScene } from '@proj-airi/stage-ui-three'
import { animations } from '@proj-airi/stage-ui-three/assets/vrm'
import { createQueue } from '@proj-airi/stream-kit'
import { useBroadcastChannel } from '@vueuse/core'
// import { createTransformers } from '@xsai-transformers/embed'
// import embedWorkerURL from '@xsai-transformers/embed/worker?worker&url'
// import { embed } from '@xsai/embed'
import { storeToRefs } from 'pinia'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import { useDelayMessageQueue, useEmotionsMessageQueue } from '../../composables/queues'
import { useAuthProviderSync } from '../../composables/use-auth-provider-sync'
import { llmInferenceEndToken } from '../../constants'
import { EMOTION_EmotionMotionName_value, EMOTION_VRMExpressionName_value, EmotionThinkMotionName } from '../../constants/emotions'
import { useAudioContext, useSpeakingStore } from '../../stores/audio'
import { useChatOrchestratorStore } from '../../stores/chat'
import { useAiriCardStore } from '../../stores/modules'
import { useSpeechStore } from '../../stores/modules/speech'
import { useProvidersStore } from '../../stores/providers'
import { useSettings } from '../../stores/settings'
import { useSpeechRuntimeStore } from '../../stores/speech-runtime'
import { useTtsProgressStore } from '../../stores/tts-progress'
import { shouldRunLive2dLipSyncLoop } from './runtime'

const props = withDefaults(defineProps<{
  paused?: boolean
  focusAt: { x: number, y: number }
  xOffset?: number | string
  yOffset?: number | string
  scale?: number
}>(), { paused: false, scale: 1 })

const componentState = defineModel<'pending' | 'loading' | 'mounted'>('state', { default: 'pending' })

const db = ref<DuckDBWasmDrizzleDatabase>()
// const transformersProvider = createTransformers({ embedWorkerURL })

const vrmViewerRef = ref<InstanceType<typeof ThreeScene>>()
const live2dSceneRef = ref<InstanceType<typeof Live2DScene>>()

const settingsStore = useSettings()
const {
  stageModelRenderer,
  stageViewControlsEnabled,
  live2dDisableFocus,
  stageModelSelectedUrl,
  stageModelSelected,
  themeColorsHue,
  themeColorsHueDynamic,
  live2dIdleAnimationEnabled,
  live2dAutoBlinkEnabled,
  live2dForceAutoBlinkEnabled,
  live2dShadowEnabled,
  live2dMaxFps,
} = storeToRefs(settingsStore)
const { mouthOpenSize } = storeToRefs(useSpeakingStore())
const { audioContext } = useAudioContext()
const currentAudioSource = ref<AudioBufferSourceNode>()

const { onBeforeMessageComposed, onBeforeSend, onTokenLiteral, onTokenSpecial, onStreamEnd, onAssistantResponseEnd } = useChatOrchestratorStore()
const chatHookCleanups: Array<() => void> = []
// WORKAROUND: clear previous handlers on unmount to avoid duplicate calls when this component remounts.
//             We keep per-hook disposers instead of wiping the global chat hooks to play nicely with
//             cross-window broadcast wiring.

const providersStore = useProvidersStore()
useAuthProviderSync()
const live2dStore = useLive2d()
const showStage = ref(true)
const viewUpdateCleanups: Array<() => void> = []

// Caption + Presentation broadcast channels
type CaptionChannelEvent
  = | { type: 'caption-speaker', text: string }
    | { type: 'caption-assistant', text: string }
const { post: postCaption } = useBroadcastChannel<CaptionChannelEvent, CaptionChannelEvent>({ name: 'airi-caption-overlay' })
const assistantCaption = ref('')

type PresentEvent
  = | { type: 'assistant-reset' }
    | { type: 'assistant-append', text: string }
const { post: postPresent } = useBroadcastChannel<PresentEvent, PresentEvent>({ name: 'airi-chat-present' })

viewUpdateCleanups.push(live2dStore.onShouldUpdateView(async () => {
  showStage.value = false
  await settingsStore.updateStageModel()
  setTimeout(() => {
    showStage.value = true
  }, 100)
}))

const audioAnalyser = ref<AnalyserNode>()
const nowSpeaking = ref(false)
const lipSyncStarted = ref(false)
const lipSyncLoopId = ref<number>()
const live2dLipSync = ref<Live2DLipSync>()
const live2dLipSyncOptions: Live2DLipSyncOptions = { mouthUpdateIntervalMs: 50, mouthLerpWindowMs: 50 }

const { activeCard } = storeToRefs(useAiriCardStore())
const speechStore = useSpeechStore()
const { ssmlEnabled, activeSpeechProvider, activeSpeechModel, activeSpeechVoice, pitch } = storeToRefs(speechStore)
const activeCardId = computed(() => activeCard.value?.name ?? 'default')
const speechRuntimeStore = useSpeechRuntimeStore()

const { currentMotion } = storeToRefs(useLive2d())

const emotionsQueue = createQueue<EmotionPayload>({
  handlers: [
    async (ctx) => {
      if (stageModelRenderer.value === 'vrm') {
        // console.debug('VRM emotion anime: ', ctx.data)
        const value = EMOTION_VRMExpressionName_value[ctx.data.name]
        if (!value)
          return

        await vrmViewerRef.value!.setExpression(value, ctx.data.intensity)
      }
      else if (stageModelRenderer.value === 'live2d') {
        currentMotion.value = { group: EMOTION_EmotionMotionName_value[ctx.data.name] }
      }
    },
  ],
})

const emotionMessageContentQueue = useEmotionsMessageQueue(emotionsQueue)
emotionMessageContentQueue.onHandlerEvent('emotion', (emotion) => {
  // eslint-disable-next-line no-console
  console.debug('emotion detected', emotion)
})

const delaysQueue = useDelayMessageQueue()
delaysQueue.onHandlerEvent('delay', (delay) => {
  // eslint-disable-next-line no-console
  console.debug('delay detected', delay)
})

// Play special token: delay or emotion
function playSpecialToken(special: string) {
  delaysQueue.enqueue(special)
  emotionMessageContentQueue.enqueue(special)
}
const lipSyncNode = ref<AudioNode>()

async function playFunction(item: Parameters<Parameters<typeof createPlaybackManager<AudioBuffer>>[0]['play']>[0], signal: AbortSignal): Promise<void> {
  if (!audioContext || !item.audio)
    return

  // Ensure audio context is resumed (browsers suspend it by default until user interaction)
  if (audioContext.state === 'suspended') {
    try {
      await audioContext.resume()
    }
    catch {
      return
    }
  }

  const source = audioContext.createBufferSource()
  currentAudioSource.value = source
  source.buffer = item.audio

  source.connect(audioContext.destination)
  if (audioAnalyser.value)
    source.connect(audioAnalyser.value)
  if (lipSyncNode.value)
    source.connect(lipSyncNode.value)

  return new Promise<void>((resolve) => {
    let settled = false
    const resolveOnce = () => {
      if (settled)
        return
      settled = true
      resolve()
    }

    const stopPlayback = () => {
      try {
        source.stop()
        source.disconnect()
      }
      catch {}
      if (currentAudioSource.value === source)
        currentAudioSource.value = undefined
      resolveOnce()
    }

    if (signal.aborted) {
      stopPlayback()
      return
    }

    signal.addEventListener('abort', stopPlayback, { once: true })
    source.onended = () => {
      signal.removeEventListener('abort', stopPlayback)
      stopPlayback()
    }

    try {
      source.start(0)
    }
    catch {
      stopPlayback()
    }
  })
}

const playbackManager = createPlaybackManager<AudioBuffer>({
  play: playFunction,
  maxVoices: 1,
  maxVoicesPerOwner: 1,
  overflowPolicy: 'queue',
  ownerOverflowPolicy: 'steal-oldest',
})

// NOTICE: For local TTS providers (e.g. kokoro-local), buffer the entire response
// into a single TTS call to avoid per-chunk WASM inference overhead. Cloud providers
// use the default sentence-level chunking for low-latency streaming.
const LOCAL_TTS_PROVIDERS = new Set(['kokoro-local'])

function createSegmenterForProvider() {
  return (tokens: ReadableStream<TextToken>, meta: { streamId: string, intentId: string }) => {
    if (!LOCAL_TTS_PROVIDERS.has(activeSpeechProvider.value ?? '')) {
      return createTtsSegmentStream(tokens, meta)
    }

    // For local TTS: collect all tokens, then emit a single segment on flush/end.
    const { stream, write, close } = createPushStream<TextSegment>()
    let buffer = ''
    const specials: string[] = []

    void (async () => {
      const reader = tokens.getReader()
      try {
        while (true) {
          const { value, done } = await reader.read()
          if (done)
            break
          if (!value)
            continue

          if (value.type === 'literal') {
            buffer += value.value ?? ''
          }
          else if (value.type === 'special') {
            // Emit specials as separate segments so emotions still fire mid-stream
            specials.push(value.value ?? '')
            write({
              streamId: meta.streamId,
              intentId: meta.intentId,
              segmentId: `${meta.streamId}:special:${Date.now()}`,
              text: '',
              special: value.value ?? null,
              reason: 'special',
              createdAt: Date.now(),
            })
          }
          else if (value.type === 'flush') {
            // Flush: emit accumulated text as a single chunk
            if (buffer.trim()) {
              write({
                streamId: meta.streamId,
                intentId: meta.intentId,
                segmentId: `${meta.streamId}:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`,
                text: buffer.trim(),
                special: null,
                reason: 'flush',
                createdAt: Date.now(),
              })
              buffer = ''
            }
          }
        }
        // End of stream: emit remaining text
        if (buffer.trim()) {
          write({
            streamId: meta.streamId,
            intentId: meta.intentId,
            segmentId: `${meta.streamId}:final:${Date.now()}`,
            text: buffer.trim(),
            special: null,
            reason: 'flush',
            createdAt: Date.now(),
          })
        }
      }
      catch (err) {
        console.error('[Stage] Local TTS segmenter error:', err)
      }
      finally {
        reader.releaseLock()
        close()
      }
    })()

    return stream
  }
}

const speechPipeline = createSpeechPipeline<AudioBuffer>({
  segmenter: createSegmenterForProvider(),
  tts: async (request, signal) => {
    // NOTICE: Routed through Portkey AI gateway → Azure OpenAI TTS
    const TTS_VOICE = 'alloy'

    // eslint-disable-next-line no-console
    console.debug('[TTS] tts() called', { text: request.text?.slice(0, 80), aborted: signal.aborted })

    if (signal.aborted)
      return null
    if (!request.text && !request.special)
      return null

    try {
      const res = await fetch('https://api.portkey.ai/v1/audio/speech', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-portkey-api-key': '1JZp0JDG2yPwqJt3QeO4E2ioNnak',
          'x-portkey-virtual-key': 'swedencentral-azure-openai',
        },
        body: JSON.stringify({
          model: 'tts-hd',
          input: request.text,
          voice: TTS_VOICE,
          response_format: 'wav',
          speed: 1.0,
        }),
        signal,
      })

      if (!res.ok) {
        console.error('[TTS] Server returned', res.status, await res.text().catch(() => ''))
        return null
      }

      const arrayBuffer = await res.arrayBuffer()
      // eslint-disable-next-line no-console
      console.debug('[TTS] Got audio', { byteLength: arrayBuffer.byteLength })

      if (signal.aborted || !arrayBuffer || arrayBuffer.byteLength === 0)
        return null

      const audioBuffer = await audioContext.decodeAudioData(arrayBuffer)
      // eslint-disable-next-line no-console
      console.debug('[TTS] Audio decoded', { duration: audioBuffer.duration, sampleRate: audioBuffer.sampleRate })
      return audioBuffer
    }
    catch (err) {
      if (signal.aborted)
        return null
      console.error('[TTS] Failed:', err)
      return null
    }
  },
  playback: playbackManager,
})

void speechRuntimeStore.registerHost(speechPipeline)

// Wire TTS progress tracking
const ttsProgressStore = useTtsProgressStore()
speechPipeline.on('onIntentStart', () => ttsProgressStore.onIntentStart())
speechPipeline.on('onTtsRequest', () => ttsProgressStore.onTtsRequest())
speechPipeline.on('onTtsResult', () => ttsProgressStore.onTtsResult())
speechPipeline.on('onIntentEnd', () => ttsProgressStore.onIntentEnd())
speechPipeline.on('onIntentCancel', () => ttsProgressStore.onIntentCancel())

speechPipeline.on('onSpecial', (segment) => {
  if (segment.special)
    playSpecialToken(segment.special)
})

playbackManager.onEnd(({ item }) => {
  if (item.special)
    playSpecialToken(item.special)

  nowSpeaking.value = false
  mouthOpenSize.value = 0
})

playbackManager.onStart(({ item }) => {
  nowSpeaking.value = true
  // NOTICE: postCaption and postPresent may throw errors if the BroadcastChannel is closed
  // (e.g., when navigating away from the page). We wrap these in try-catch to prevent
  // breaking playback when the channel is unavailable.
  assistantCaption.value += ` ${item.text}`
  try {
    postCaption({ type: 'caption-assistant', text: assistantCaption.value })
  }
  catch {
    // BroadcastChannel may be closed - don't break playback
  }
  try {
    postPresent({ type: 'assistant-append', text: item.text })
  }
  catch {
    // BroadcastChannel may be closed - don't break playback
  }
})

function startLipSyncLoop() {
  if (lipSyncLoopId.value)
    return

  const tick = () => {
    if (!nowSpeaking.value || !live2dLipSync.value) {
      mouthOpenSize.value = 0
    }
    else {
      mouthOpenSize.value = live2dLipSync.value.getMouthOpen()
    }
    lipSyncLoopId.value = requestAnimationFrame(tick)
  }

  lipSyncLoopId.value = requestAnimationFrame(tick)
}

function stopLipSyncLoop() {
  if (lipSyncLoopId.value) {
    cancelAnimationFrame(lipSyncLoopId.value)
    lipSyncLoopId.value = undefined
  }

  mouthOpenSize.value = 0
}

function resetLive2dLipSync() {
  stopLipSyncLoop()

  try {
    lipSyncNode.value?.disconnect()
  }
  catch {

  }

  lipSyncNode.value = undefined
  live2dLipSync.value = undefined
  lipSyncStarted.value = false
}

function syncLipSyncLoop() {
  if (shouldRunLive2dLipSyncLoop({
    stageModelRenderer: stageModelRenderer.value,
    paused: Boolean(props.paused),
  }) && lipSyncStarted.value) {
    startLipSyncLoop()
    return
  }

  stopLipSyncLoop()
}

async function setupLipSync() {
  if (stageModelRenderer.value !== 'live2d') {
    resetLive2dLipSync()
    return
  }

  if (lipSyncStarted.value)
    return

  try {
    const lipSync = await createLive2DLipSync(audioContext, wlipsyncProfile as Profile, live2dLipSyncOptions)
    live2dLipSync.value = lipSync
    lipSyncNode.value = lipSync.node
    await audioContext.resume()
    lipSyncStarted.value = true
    syncLipSyncLoop()
  }
  catch (error) {
    resetLive2dLipSync()
    console.error('Failed to setup Live2D lip sync', error)
  }
}

function setupAnalyser() {
  if (!audioAnalyser.value) {
    audioAnalyser.value = audioContext.createAnalyser()
  }
}

let currentChatIntent: ReturnType<typeof speechRuntimeStore.openIntent> | null = null

chatHookCleanups.push(onBeforeMessageComposed(async () => {
  // eslint-disable-next-line no-console
  console.debug('[Stage] onBeforeMessageComposed', {
    provider: activeSpeechProvider.value,
    model: activeSpeechModel.value,
    voiceId: activeSpeechVoice.value?.id,
    voiceObj: !!activeSpeechVoice.value,
    renderer: stageModelRenderer.value,
  })
  playbackManager.stopAll('new-message')

  setupAnalyser()
  await setupLipSync()
  // Reset assistant caption for a new message
  assistantCaption.value = ''
  try {
    postCaption({ type: 'caption-assistant', text: '' })
  }
  catch (error) {
    // BroadcastChannel may be closed if user navigated away - don't break flow
    console.warn('[Stage] Failed to post caption reset (channel may be closed)', { error })
  }
  try {
    postPresent({ type: 'assistant-reset' })
  }
  catch (error) {
    // BroadcastChannel may be closed if user navigated away - don't break flow
    console.warn('[Stage] Failed to post present reset (channel may be closed)', { error })
  }

  if (currentChatIntent) {
    currentChatIntent.cancel('new-message')
    currentChatIntent = null
  }

  currentChatIntent = speechRuntimeStore.openIntent({
    ownerId: activeCardId.value,
    priority: 'normal',
    behavior: 'queue',
  })
}))

chatHookCleanups.push(onBeforeSend(async () => {
  currentMotion.value = { group: EmotionThinkMotionName }
}))

chatHookCleanups.push(onTokenLiteral(async (literal) => {
  // eslint-disable-next-line no-console
  console.debug('[Stage] onTokenLiteral', { literal: literal?.slice(0, 40), hasIntent: !!currentChatIntent })
  currentChatIntent?.writeLiteral(literal)
}))

chatHookCleanups.push(onTokenSpecial(async (special) => {
  // eslint-disable-next-line no-console
  console.debug('[Stage] onTokenSpecial', { special, hasIntent: !!currentChatIntent })
  currentChatIntent?.writeSpecial(special)
}))

chatHookCleanups.push(onStreamEnd(async () => {
  // eslint-disable-next-line no-console
  console.debug('[Stage] onStreamEnd, flushing intent')
  delaysQueue.enqueue(llmInferenceEndToken)
  currentChatIntent?.writeFlush()
}))

chatHookCleanups.push(onAssistantResponseEnd(async (_message) => {
  // eslint-disable-next-line no-console
  console.debug('[Stage] onAssistantResponseEnd, ending intent')
  currentChatIntent?.end()
  currentChatIntent = null
  // const res = await embed({
  //   ...transformersProvider.embed('Xenova/nomic-embed-text-v1'),
  //   input: message,
  // })

  // await db.value?.execute(`INSERT INTO memory_test (vec) VALUES (${JSON.stringify(res.embedding)});`)
}))

// Resume audio context on first user interaction (browser requirement)
let audioContextResumed = false
function resumeAudioContextOnInteraction() {
  if (audioContextResumed || !audioContext)
    return
  audioContextResumed = true
  audioContext.resume().catch(() => {
    // Ignore errors - audio context will be resumed when needed
  })
}

// Add event listeners for user interaction
if (typeof window !== 'undefined') {
  const events = ['click', 'touchstart', 'keydown']
  events.forEach((event) => {
    window.addEventListener(event, resumeAudioContextOnInteraction, { once: true, passive: true })
  })
}

onMounted(async () => {
  db.value = drizzle({ connection: { bundles: getImportUrlBundles() } })
  await db.value.execute(`CREATE TABLE memory_test (vec FLOAT[768]);`)
})

watch([stageModelRenderer, stageModelSelectedUrl], ([renderer, url]) => {
  // eslint-disable-next-line no-console
  console.debug('[Stage] Model state changed', { renderer, url: url?.slice(0, 80), showStage: showStage.value })
})

watch([stageModelRenderer, () => props.paused], ([renderer]) => {
  if (renderer !== 'live2d') {
    resetLive2dLipSync()
    return
  }

  syncLipSyncLoop()
}, { immediate: true })

function canvasElement() {
  if (stageModelRenderer.value === 'live2d')
    return live2dSceneRef.value?.canvasElement()

  else if (stageModelRenderer.value === 'vrm')
    return vrmViewerRef.value?.canvasElement()
}

function readRenderTargetRegionAtClientPoint(clientX: number, clientY: number, radius: number) {
  if (stageModelRenderer.value !== 'vrm')
    return null

  return vrmViewerRef.value?.readRenderTargetRegionAtClientPoint?.(clientX, clientY, radius) ?? null
}

onUnmounted(() => {
  resetLive2dLipSync()
  chatHookCleanups.forEach(dispose => dispose?.())
  viewUpdateCleanups.forEach(dispose => dispose?.())
})

defineExpose({
  canvasElement,
  readRenderTargetRegionAtClientPoint,
})
</script>

<template>
  <div relative h-full w-full>
    <div h-full w-full>
      <Live2DScene
        v-if="stageModelRenderer === 'live2d' && showStage"
        ref="live2dSceneRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100"
        h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :model-id="stageModelSelected"
        :focus-at="focusAt"
        :mouth-open-size="mouthOpenSize"
        :paused="paused"
        :x-offset="xOffset"
        :y-offset="yOffset"
        :scale="scale"
        :disable-focus-at="live2dDisableFocus"
        :theme-colors-hue="themeColorsHue"
        :theme-colors-hue-dynamic="themeColorsHueDynamic"
        :live2d-idle-animation-enabled="live2dIdleAnimationEnabled"
        :live2d-auto-blink-enabled="live2dAutoBlinkEnabled"
        :live2d-force-auto-blink-enabled="live2dForceAutoBlinkEnabled"
        :live2d-shadow-enabled="live2dShadowEnabled"
        :live2d-max-fps="live2dMaxFps"
      />
      <ThreeScene
        v-if="stageModelRenderer === 'vrm' && showStage"
        ref="vrmViewerRef"
        v-model:state="componentState"
        min-w="50% <lg:full" min-h="100 sm:100" h-full w-full flex-1
        :model-src="stageModelSelectedUrl"
        :idle-animation="animations.idleLoop.toString()"
        :paused="paused"
        :show-axes="stageViewControlsEnabled"
        :current-audio-source="currentAudioSource"
        @error="console.error"
      />
    </div>
  </div>
</template>
