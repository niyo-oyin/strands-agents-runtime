// interventions = ["policy.cedar"] で読み込まれる Cedar ポリシー（Strands の CedarAuthorization）。
// principal はエージェント、action はツール名、resource はツール入力。ポリシーに一致しない呼び出しは拒否される。

// すべてのツール呼び出しを許可する。
permit(principal, action, resource);

// 特定のツールだけを許可する例:
// permit(principal, action == Action::"hello", resource);
