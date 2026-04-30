---
name: flutter-patterns
description: >
  Flutter/Dart best practices. Dùng khi Dev viết code cho Jarvis frontend:
  state management, Material Design, widget patterns.
---

# FLUTTER PATTERNS CHO JARVIS

## Widget Structure
```dart
// ✅ StatelessWidget cho UI tĩnh
class AgentCard extends StatelessWidget {
  final String name;
  final String status;
  const AgentCard({required this.name, required this.status, super.key});

  @override
  Widget build(BuildContext context) {
    return Card(
      child: ListTile(
        title: Text(name),
        subtitle: Text(status),
      ),
    );
  }
}
```

## State Management (Provider/Riverpod)
```dart
// Service class
class AgentService {
  final String baseUrl;
  AgentService(this.baseUrl);

  Future<List<Agent>> getAgents() async {
    final response = await http.get(Uri.parse('$baseUrl/api/v1/agents'));
    return (jsonDecode(response.body) as List)
        .map((e) => Agent.fromJson(e))
        .toList();
  }
}
```

## Navigation
```dart
// Named routes
MaterialApp(
  routes: {
    '/': (context) => HomeScreen(),
    '/agents': (context) => AgentsScreen(),
    '/chat': (context) => ChatScreen(),
  },
);
```

## Error Handling
```dart
// ✅ FutureBuilder with error states
FutureBuilder<List<Agent>>(
  future: agentService.getAgents(),
  builder: (context, snapshot) {
    if (snapshot.hasError) return ErrorWidget(snapshot.error!);
    if (!snapshot.hasData) return CircularProgressIndicator();
    return AgentList(agents: snapshot.data!);
  },
);
```

## Styling
- Dùng `Theme.of(context)` thay vì hardcode colors
- Responsive: `LayoutBuilder` + `MediaQuery`
- Material 3 design tokens
