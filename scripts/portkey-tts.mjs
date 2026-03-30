import fs from 'node:fs'
import path from 'node:path'

import OpenAI from 'openai'

import { createHeaders, PORTKEY_GATEWAY_URL } from 'portkey-ai'

const PORTKEY_API_KEY = process.env.PORTKEY_KEY || '1JZp0JDG2yPwqJt3QeO4E2ioNnak'

const openai = new OpenAI({
  apiKey: 'dummy',
  baseURL: PORTKEY_GATEWAY_URL,
  defaultHeaders: createHeaders({
    apiKey: PORTKEY_API_KEY,
    virtualKey: 'swedencentral-azure-openai',
  }),
})

const speechFile = path.resolve('./speech.mp3')

async function main() {
  const text = process.argv[2] || 'Today is a wonderful day to build something people love!'
  console.log(`Generating speech for: "${text}"`)

  const mp3 = await openai.audio.speech.create({
    model: 'tts-hd',
    voice: 'alloy',
    input: text,
  })

  const buffer = Buffer.from(await mp3.arrayBuffer())
  await fs.promises.writeFile(speechFile, buffer)
  console.log(`Saved to ${speechFile}`)
}

main().catch(console.error)
