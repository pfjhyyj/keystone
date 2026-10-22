import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { Sha256 } from '../services/upload/sha256'

describe('bounded incremental SHA-256', () => {
  it.each([0, 1, 55, 56, 63, 64, 65, 127, 128, 129, 1000000])(
    'matches the platform digest for %i bytes across uneven chunks',
    (n) => {
      const bytes = Uint8Array.from({ length: n }, (_, i) => i % 251)
      const hash = new Sha256()
      for (let offset = 0; offset < n; offset += 37)
        hash.update(bytes.subarray(offset, offset + 37))
      expect(hash.digest()).toBe(createHash('sha256').update(bytes).digest('hex'))
    },
  )
  it('handles the high 32-bit bit count without holding a large file', () => {
    const block = new Uint8Array(1024 * 1024).fill(97),
      hash = new Sha256(),
      expected = createHash('sha256')
    for (let i = 0; i < 513; i++) {
      hash.update(block)
      expected.update(block)
    }
    expect(hash.digest()).toBe(expected.digest('hex'))
  }, 60000)
})
