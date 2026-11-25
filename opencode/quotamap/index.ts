import { Plugin } from "@opencode/plugin"
import { register } from "./core.mjs"

export default Plugin.define({
  id: "quotamap",
  async setup(ctx: any) {
    await register(ctx)
  },
})
