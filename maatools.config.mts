import type { FullConfig } from '@nekosu/maa-tools'

const config: FullConfig = {
  cwd: import.meta.dirname,
  check: {
    override: {
      'dynamic-image': 'ignore',
    },
  },
}

export default config