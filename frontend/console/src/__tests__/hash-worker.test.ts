import { createHash } from 'node:crypto'
import { expect, it, vi } from 'vitest'

it('reads actual hash worker input in bounded slices and never reads the entire file', async () => {
  const messages = vi.fn()
  const scope = {
    postMessage: messages,
    onmessage: null as ((event: MessageEvent<File>) => Promise<void>) | null,
  }
  vi.stubGlobal('self', scope)
  await import('../services/upload/hash.worker')
  const size = 8 * 1024 * 1024 + 17
  const slice = vi.fn((start: number, end: number) => ({
    arrayBuffer: async () => new Uint8Array(end - start).fill(97).buffer,
  }))
  const whole = vi.fn(() => {
    throw new Error('Whole-file allocation is forbidden')
  })
  const file = new File([], 'large.txt')
  Object.defineProperties(file, {
    size: { value: size },
    slice: { value: slice },
    arrayBuffer: { value: whole },
  })
  await scope.onmessage!(new MessageEvent('message', { data: file }))
  expect(whole).not.toHaveBeenCalled()
  expect(slice.mock.calls.every(([start, end]) => end - start <= 4 * 1024 * 1024)).toBe(true)
  expect(messages).toHaveBeenLastCalledWith({
    checksum: createHash('sha256').update(Buffer.alloc(size, 97)).digest('hex'),
  })
  expect(messages).toHaveBeenCalledWith({ loaded: size })
})
