package uz.miya.companion.watch

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import uz.miya.companion.work.Scheduling

/**
 * Wake-up only (build step 6). A read-only SMS_RECEIVED receipt needs no
 * default-SMS role, but a receiver gets ~10 seconds on the main thread — so
 * this does NOTHING but enqueue the event-sync work and return. The worker
 * reads the SMS provider itself, which is also why nothing is lost while the
 * app is force-stopped or this broadcast is missed: the provider keeps the
 * message and the next sweep picks it up by _ID.
 *
 * The payload of the broadcast is deliberately ignored — parsing PDUs here
 * would mean a second, subtly different reading of the same message.
 */
class SmsReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        Scheduling.enqueueEventSync(context)
    }
}
