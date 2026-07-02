export class MemoryStore {
  constructor() {
    this.notes = [];
    this.todos = [];
    this.events = [];
  }

  addNote(text) {
    const note = {
      id: `note_${this.notes.length + 1}`,
      text,
      createdAt: new Date().toISOString(),
    };
    this.notes.push(note);
    return note;
  }

  addTodo(text) {
    const todo = {
      id: `todo_${this.todos.length + 1}`,
      text,
      done: false,
      createdAt: new Date().toISOString(),
    };
    this.todos.push(todo);
    return todo;
  }

  listTodos() {
    return [...this.todos];
  }

  recordEvent(event) {
    this.events.push({
      ...event,
      createdAt: new Date().toISOString(),
    });
  }

  snapshot() {
    return {
      notes: [...this.notes],
      todos: [...this.todos],
      recentEvents: this.events.slice(-10),
    };
  }
}
