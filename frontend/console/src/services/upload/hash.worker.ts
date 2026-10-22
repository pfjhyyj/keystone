import { Sha256 } from './sha256'
export const HASH_CHUNK_BYTES = 4 * 1024 * 1024
self.onmessage = async (event: MessageEvent<File>) => {
  try {
    const file = event.data,
      hash = new Sha256()
    for (let offset = 0; offset < file.size; offset += HASH_CHUNK_BYTES) {
      const end = Math.min(file.size, offset + HASH_CHUNK_BYTES)
      hash.update(new Uint8Array(await file.slice(offset, end).arrayBuffer()))
      self.postMessage({ loaded: end })
    }
    self.postMessage({ checksum: hash.digest() })
  } catch {
    self.postMessage({ error: '无法读取本地文件，请重新选择' })
  }
}
