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
    Mencatat Jurnal Penjualan, HPP, dan Mengurangi Stok Fisik.
    """
    try:
        # 1. CEK DUPLIKASI (IDEMPOTENCY)
        # Mencegah stok terpotong 2x jika webhook terpanggil ganda
        existing = supabase.table("journal_entries").select("id").eq("order_id", order_id).execute()
        if existing.data:
            print(f"INFO: Jurnal untuk Order {order_id} sudah ada. Skip.")
            return True

        # 2. Ambil Detail Pesanan dan Produk
        # [PENTING] Kita tambahkan kolom 'stock' di select query agar tahu stok saat ini
        order_response = supabase.table("orders").select(
            "*, order_items(*, products(id, cost_price, inventory_account_code, hpp_account_code, stock))"
        ).eq("id", order_id).execute()
        
        if not order_response.data:
            print(f"ERROR: Order {order_id} tidak ditemukan.")
            return False

        order = order_response.data[0]
        total_revenue = order["total_amount"]
        
        CASH_ACCOUNT = '1-1100'
        SALES_ACCOUNT = '4-1100'
        
        lines = []
        movements_to_insert = []

        # 3. HEADER JURNAL (Tipe REGULAR)
        journal = supabase.table("journal_entries").insert({
            "order_id": order_id,
            "transaction_date": str(date.today()),
            "description": f"Jurnal Penjualan Tunai Order ID: {order_id}",
            "user_id": order.get("user_id"),
            "entry_type": "REGULAR" 
        }).execute().data[0]
        journal_id = journal["id"]

        # 4. DEBIT KAS & KREDIT PENJUALAN
        lines.append({"journal_id": journal_id, "account_code": CASH_ACCOUNT, "debit_amount": total_revenue, "credit_amount": 0})
        lines.append({"journal_id": journal_id, "account_code": SALES_ACCOUNT, "debit_amount": 0, "credit_amount": total_revenue})
        
        # 5. LOOP ITEMS: JURNAL HPP & UPDATE STOK
        for item in order["order_items"]:
            product_id = item["product_id"]
            quantity_sold = item["quantity"]
            
            product_data = item.get("products", {})
            cost_price = product_data.get("cost_price", 0) or 0
            inventory_acc = product_data.get("inventory_account_code", '1-1200')
            hpp_acc = product_data.get("hpp_account_code", '5-1100')
            
            # Ambil stok saat ini dari database
            current_stock = product_data.get("stock", 0)

            if quantity_sold > 0:
                cost_of_sale = quantity_sold * cost_price

                # A. Jurnal HPP (Jika ada HPP)
                if cost_price > 0:
                    lines.append({"journal_id": journal_id, "account_code": hpp_acc, "debit_amount": cost_of_sale, "credit_amount": 0})
                    lines.append({"journal_id": journal_id, "account_code": inventory_acc, "debit_amount": 0, "credit_amount": cost_of_sale})

                # B. Catat Inventory Movement (History)
                movements_to_insert.append({
                    "product_id": product_id,
                    "movement_date": str(date.today()), 
                    "movement_type": "ISSUE", 
                    "quantity_change": -quantity_sold, 
                    "unit_cost": cost_price,
                    "reference_id": f"ORDER-{order_id}",
                })
                
                # C. [FIX UTAMA] KURANGI STOK FISIK DI TABEL PRODUCTS
                new_stock = current_stock - quantity_sold
                # Pastikan stok tidak negatif (opsional, tergantung kebijakan)
                if new_stock < 0: new_stock = 0 
                
                # Update ke Supabase
                update_res = supabase.table("products").update({"stock": new_stock}).eq("id", product_id).execute()
                print(f"Update Stok Produk {product_id}: {current_stock} -> {new_stock}")

        # 6. Simpan Semua Perubahan
        if lines:
            supabase.table("journal_lines").insert(lines).execute()
        
        if movements_to_insert:
            supabase.table("inventory_movements").insert(movements_to_insert).execute()

        print(f"SUCCESS: Order {order_id} selesai diproses (Jurnal + Stok).")
        return True

    except Exception as e:
        print(f"FATAL ERROR Processing Order {order_id}: {e}")
        return False

# ===============================================
# MIDTRANS WEBHOOK
# ===============================================

@app.post("/midtrans/notification")
async def midtrans_notification(request: Request):
    try:
        payload = await request.json()
        raw_order_id = str(payload.get("order_id", ""))
        
        if "-" in raw_order_id:
            order_id = raw_order_id.split("-")[0]
        else:
            order_id = raw_order_id
            
        transaction_status = payload.get("transaction_status")
        transaction_id = payload.get("transaction_id")
        
        if not order_id:
            raise HTTPException(status_code=400, detail="Missing order_id")

        print(f"Webhook masuk: Order {order_id} | Status: {transaction_status}")

        new_status = transaction_status
        journal_recorded = False

        if transaction_status in ["capture", "settlement"]:
            new_status = "settle"
            # Jalankan pencatatan jurnal & pengurangan stok
            journal_recorded = record_sales_journal(int(order_id)) 
            
        elif transaction_status in ["deny", "expire", "cancel"]:
            new_status = "failed"
            
        # Update status order
        supabase.table("orders").update({
            "status": new_status,
            "midtrans_order_id": transaction_id 
        }).eq("id", int(order_id)).execute()

        return {"status": "ok", "processed": journal_recorded}

    except Exception as e:
        print(f"Webhook Error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

if __name__ == "__main__":
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8080)
