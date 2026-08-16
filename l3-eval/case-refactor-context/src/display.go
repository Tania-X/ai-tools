// Package main 提供渲染示例(既有代码, 供 case-refactor-context 场景验证跨文件影响)。
package main

// render 渲染用户问候语(期望 getUserName 返回完整姓名)。
func render(id string) string {
	return "Hello, " + getUserName(id)
}
