import type { DisplayModel } from '../display-models'

import { useLocalStorageManualReset } from '@proj-airi/stage-shared/composables'
import { refManualReset, useEventListener } from '@vueuse/core'
import { defineStore } from 'pinia'
import { computed, watch } from 'vue'

import { DisplayModelFormat, useDisplayModelsStore } from '../display-models'

export type StageModelRenderer = 'live2d' | 'vrm' | 'disabled' | undefined

/**
 * Stage model settings — controls which 3D/2D avatar model is rendered on the main stage.
 *
 * The `stageModelSelected` ID is persisted in localStorage and looked up against
 * the `DisplayModelsStore` (presets + user-imported models). When the selected model
 * changes, `updateStageModel()` resolves the model definition, sets the renderer
 * type (`live2d` | `vrm` | `disabled`), and generates a URL for the scene component.
 *
 * Default: `preset-vrm-tieguy` (Tieguy). Changed via settings > scene > "Select Model".
 */
export const useSettingsStageModel = defineStore('settings-stage-model', () => {
  const displayModelsStore = useDisplayModelsStore()
  let stageModelUpdateSequence = 0
  const stageModelStorageKey = 'settings/stage/model'

  const stageModelSelectedState = useLocalStorageManualReset<string>(stageModelStorageKey, 'preset-vrm-tieguy')
  const stageModelSelected = computed<string>({
    get: () => stageModelSelectedState.value,
    set: (value) => {
      stageModelSelectedState.value = value
    },
  })
  const stageModelSelectedDisplayModel = refManualReset<DisplayModel | undefined>(undefined)
  const stageModelSelectedUrl = refManualReset<string | undefined>(undefined)
  const stageModelRenderer = refManualReset<StageModelRenderer>(undefined)

  const stageViewControlsEnabled = refManualReset<boolean>(false)

  function revokeStageModelUrl(url?: string) {
    if (url?.startsWith('blob:'))
      URL.revokeObjectURL(url)
  }

  function replaceStageModelUrl(nextUrl?: string) {
    if (stageModelSelectedUrl.value === nextUrl)
      return

    revokeStageModelUrl(stageModelSelectedUrl.value)
    stageModelSelectedUrl.value = nextUrl
  }

  async function updateStageModel() {
    const requestId = ++stageModelUpdateSequence
    const selectedModelId = stageModelSelectedState.value

    // eslint-disable-next-line no-console
    console.debug('[StageModel] updateStageModel called', { selectedModelId, requestId })

    if (!selectedModelId) {
      // eslint-disable-next-line no-console
      console.debug('[StageModel] No model selected, disabling renderer')
      replaceStageModelUrl(undefined)
      stageModelSelectedDisplayModel.value = undefined
      stageModelRenderer.value = 'disabled'
      return
    }

    const model = await displayModelsStore.getDisplayModel(selectedModelId)
    if (requestId !== stageModelUpdateSequence)
      return

    if (!model) {
      console.warn('[StageModel] Model not found for id:', selectedModelId)
      replaceStageModelUrl(undefined)
      stageModelSelectedDisplayModel.value = undefined
      stageModelRenderer.value = 'disabled'
      return
    }

    // eslint-disable-next-line no-console
    console.debug('[StageModel] Model resolved', { id: model.id, format: model.format, type: model.type })

    switch (model.format) {
      case DisplayModelFormat.Live2dZip:
        stageModelRenderer.value = 'live2d'
        break
      case DisplayModelFormat.VRM:
        stageModelRenderer.value = 'vrm'
        break
      default:
        stageModelRenderer.value = 'disabled'
        break
    }

    if (model.type === 'file') {
      const nextUrl = URL.createObjectURL(model.file)
      if (requestId !== stageModelUpdateSequence) {
        URL.revokeObjectURL(nextUrl)
        return
      }

      replaceStageModelUrl(nextUrl)
    }
    else {
      replaceStageModelUrl(model.url)
    }

    // eslint-disable-next-line no-console
    console.debug('[StageModel] Renderer set to', stageModelRenderer.value, 'URL:', stageModelSelectedUrl.value?.slice(0, 80))
    stageModelSelectedDisplayModel.value = model
  }

  async function initializeStageModel() {
    await updateStageModel()
  }

  useEventListener('unload', () => {
    revokeStageModelUrl(stageModelSelectedUrl.value)
  })

  watch(stageModelSelectedState, (_newValue, _oldValue) => {
    void updateStageModel()
  })

  async function resetState() {
    revokeStageModelUrl(stageModelSelectedUrl.value)

    stageModelSelectedState.reset()
    stageModelSelectedDisplayModel.reset()
    stageModelSelectedUrl.reset()
    stageModelRenderer.reset()
    stageViewControlsEnabled.reset()

    await updateStageModel()
  }

  return {
    stageModelRenderer,
    stageModelSelected,
    stageModelSelectedUrl,
    stageModelSelectedDisplayModel,
    stageViewControlsEnabled,

    initializeStageModel,
    updateStageModel,
    resetState,
  }
})
