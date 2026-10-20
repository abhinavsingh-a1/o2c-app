from datetime import date

from agent.contracts import make_store, make_tolerance_lookup
from agent.erp import ErpClient
from agent.materials import make_material_matcher
from agent.settings import Settings

s = Settings()
erp = ErpClient(s.erp_base_url)

customer = erp.search_customers("Salzach")[0]
print("customer:", customer["kunnr"], customer["name1"])
print("price 400103:", erp.get_price("400103", customer["kunnr"], date.today()))
print("unknown material:", erp.get_material("999999"))

tolerance = make_tolerance_lookup(s, make_store(s))(customer["kunnr"])
print("tolerance:", tolerance.tolerance_pct, "|", tolerance.evidence)

match = make_material_matcher(s)(
    "Unterlegscheibe DIN 125 A 8,4 verzinkt, 100 Stk", erp.search_materials("")
)
print("match:", match.matnr, "|", match.reason)
