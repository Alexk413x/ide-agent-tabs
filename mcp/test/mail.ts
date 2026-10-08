import { history } from '../src/messaging/history.js';
import { deliver, peekUnread, takeMessages, type Message, type MessageFilter } from '../src/messaging/mailbox.js';

export { newMessageId, type Message } from '../src/messaging/mailbox.js';

export const deliverTo = (home: string, message: Message): Promise<void> => deliver(home, message);

export const unread = (home: string, id: string): Promise<Message[]> => peekUnread(home, id);

export const take = (home: string, id: string, filter: MessageFilter = {}, limit = Infinity): Promise<Message[]> => takeMessages(home, id, filter, limit);

export async function readBy(home: string, id: string): Promise<string[]> {
  const items = await history(home, { id, names: [] });
  return items.filter((m) => m.direction === 'received' && m.status === 'read').map((m) => m.id);
}
