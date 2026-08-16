// Package main 提供 golden 测试的基线代码。
//
// 该文件是"无问题"的基线版本, 各场景在其上注入已知变更(见 scenarios/)。
package main

import "fmt"

// User 表示一个用户。
type User struct {
	ID   int
	Name string
}

// main 是程序入口。
func main() {
	fmt.Println("golden test baseline")
}
