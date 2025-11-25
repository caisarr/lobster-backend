import os
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from supabase_client import supabase 
from dotenv import load_dotenv
from datetime import date 

load_dotenv()

app = FastAPI(title="Midtrans Webhook Listener & Accounting Processor")

MIDTRANS_SERVER_KEY = os.getenv("MIDTRANS_SERVER_KEY")

# ===============================================
# FUNGSI AKUNTANSI & INVENTORY
# ===============================================

def record_sales_journal(order_id: int):
    """
    Mencatat Jurnal Penjualan, HPP, dan Pergerakan Persediaan (ISSUE).
    """
    try:
        # 1. Ambil Detail Pesanan dan Barang (Order Items)
        order_response = supabase.table("orders").select(
            "*, order_items(*, products(id, cost_price, inventory_account_code, hpp_account_code))"
        ).eq("id", order_id).execute()
        
        if not order_response.data:
            print(f"ERROR: Order ID {order_id} tidak ditemukan untuk Jurnal.")
            return False

        order = order_response.data[0]
        total_revenue = order["total_amount"]
        
        # Akun Kompleks yang Digunakan (Sesuai COA Anda)
        CASH_ACCOUNT = '1-1100'   # Kas
        SALES_ACCOUNT = '4-1100'  # Penjualan
        
        lines = []
        movements_to_insert = []

        # --- 2. JURNAL 1: PENJUALAN (CASH vs REVENUE) ---
        
        # [PERBAIKAN PENTING] Menambahkan transaction_date agar muncul di laporan
        journal = supabase.table("journal_entries").insert({
            "order_id": order_id,
            "transaction_date": str(date.today()), # <--- WAJIB ADA
            "description": f"Jurnal Penjualan Tunai Order ID: {order_id}",
            "user_id": order.get("user_id") 
        }).execute().data[0]
        journal_id = journal["id"]

        # DEBIT: KAS
        lines.append({
            "journal_id": journal_id, "account_code": CASH_ACCOUNT, 
            "debit_amount": total_revenue, "credit_amount": 0
        })
        
        # KREDIT: PENJUALAN
        lines.append({
            "journal_id": journal_id, "account_code": SALES_ACCOUNT, 
            "debit_amount": 0, "credit_amount": total_revenue
        })
        
        # --- 3. JURNAL 2 & INVENTORY MOVEMENT: HPP & STOK KELUAR ---
        
        for item in order["order_items"]:
            product_id = item["product_id"]
            quantity_sold = item["quantity"]
            
            product_data = item.get("products", {})
            cost_price = product_data.get("cost_price", 0)
            inventory_acc = product_data.get("inventory_account_code", '1-1200')
            hpp_acc = product_data.get("hpp_account_code", '5-1100')           

            if cost_price > 0 and quantity_sold > 0:
                cost_of_sale = quantity_sold * cost_price

                # DEBIT: HPP
                lines.append({
                    "journal_id": journal_id, "account_code": hpp_acc, 
                    "debit_amount": cost_of_sale, "credit_amount": 0
                })
                
                # KREDIT: PERSEDIAAN
                lines.append({
                    "journal_id": journal_id, "account_code": inventory_acc, 
                    "debit_amount": 0, "credit_amount": cost_of_sale
                })

                # CATAT INVENTORY MOVEMENT (UNIT KELUAR)
                movements_to_insert.append({
                    "product_id": product_id,
                    "movement_date": str(date.today()), 
                    "movement_type": "ISSUE", 
                    "quantity_change": -quantity_sold, 
                    "unit_cost": cost_price,
                    "reference_id": f"ORDER-{order_id}",
                })

        if lines:
            supabase.table("journal_lines").insert(lines).execute()
        
        if movements_to_insert:
            supabase.table("inventory_movements").insert(movements_to_insert).execute()

        print(f"SUCCESS: Jurnal dan Inventory Movement untuk Order {order_id} berhasil dicatat.")
        return True

    except Exception as e:
        print(f"FATAL ERROR PENCATATAN JURNAL untuk Order {order_id}: {e}")
        return False

# ===============================================
# FUNGSI MIDTRANS WEBHOOK
# ===============================================

@app.post("/midtrans/notification")
async def midtrans_notification(request: Request):
    """Endpoint Midtrans Notification URL."""
    try:
        payload = await request.json()
        
        # Ambil order_id mentah (misal: "15-1732412345")
        raw_order_id = str(payload.get("order_id", ""))
        
        # BERSIHKAN ID: Ambil bagian sebelum tanda strip (-)
        if "-" in raw_order_id:
            order_id = raw_order_id.split("-")[0]
        else:
            order_id = raw_order_id
            
        transaction_status = payload.get("transaction_status")
        transaction_id = payload.get("transaction_id")
        
        if not order_id:
            raise HTTPException(status_code=400, detail="Missing order_id in payload")

        print(f"Notifikasi diterima untuk Order ID Asli: {order_id} (Raw: {raw_order_id}). Status: {transaction_status}")

        new_status = ""
        journal_recorded = False

        if transaction_status in ["capture", "settlement"]:
            new_status = "settle"
            # Catat jurnal menggunakan ID asli yang sudah bersih
            journal_recorded = record_sales_journal(int(order_id)) 
            
        elif transaction_status == "pending":
            new_status = "pending"
        elif transaction_status in ["deny", "expire", "cancel"]:
            new_status = "failed"
        else:
            new_status = transaction_status
            
        # UPDATE STATUS DI SUPABASE MENGGUNAKAN ID ASLI
        update_response = supabase.table("orders").update({
            "status": new_status,
            "midtrans_order_id": transaction_id 
        }).eq("id", int(order_id)).execute()

        if not update_response.data:
            print(f"ERROR: Gagal memperbarui status order {order_id} di Supabase.")
            return {"status": "error", "message": "Supabase update failed but notification received"}

        return {"status": "ok", "journal_recorded": journal_recorded}

    except Exception as e:
        print(f"ERROR Processing Webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


if __name__ == "__main__":
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8080, reload=True)
