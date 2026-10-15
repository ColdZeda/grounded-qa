/**
 * 给 `RouteMeta` 补上我们自己用的字段。
 *
 * ⚠️ **这个文件必须是"模块"**（所以有下面那行 `export {}`）。
 *     在非模块的 `.d.ts` 里写 `declare module 'vue-router'`，
 *     TypeScript 会理解成**"声明一个新模块"**而不是**"扩充已有模块"** ——
 *     结果是把真的 vue-router 类型整个盖掉，`createRouter` 等全部变成 any。
 *     这个坑很隐蔽：不报错，只是类型检查静默失效。
 */

export {}

declare module 'vue-router' {
  interface RouteMeta {
    /** 不需要登录就能访问（目前只有登录页）。 */
    public?: boolean
  }
}
